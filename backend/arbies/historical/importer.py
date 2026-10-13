"""Historical order-book importer.

Supported inputs (CSV, optionally gzip-compressed), using the documented Tardis.dev dataset layouts:

* ``incremental_book_L2`` - exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount
  (amount 0 deletes a level; a run of is_snapshot=true rows after updates starts a fresh book)
* ``book_snapshot_N``     - exchange,symbol,timestamp,local_timestamp,asks[0].price,asks[0].amount,bids[0].price,...
* ``quotes``              - exchange,symbol,timestamp,local_timestamp,ask_amount,ask_price,bid_price,bid_amount

Timestamps are microseconds since the Unix epoch. ``timestamp`` (exchange time) is stored as ``exchange_ts``;
``local_timestamp`` (the provider's capture time - when a trading system could actually have known about the
update) is stored as ``received_ts``. Rows are written to ``orderbook_snapshots`` with
``source='third_party_imported'`` and a ``dataset_id`` pointing at the provenance record in
``historical_datasets``. Nothing here creates or fills data: a gap in the file stays a gap.

Import is resumable (checkpointed line offset + reconstructed book state, committed atomically with each batch)
and idempotent (a file is identified by its SHA-256; a completed file is never imported twice).
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import insert, select

from arbies.core.models import DataSource, utcnow
from arbies.db.models import HistoricalDataset, OrderBookSnapshot
from arbies.db.session import Database

QUOTES = ("USDT", "USDC", "USD", "GBP", "EUR", "BTC", "XBT", "ETH")
ALIASES = {"XBT": "BTC", "XXBT": "BTC", "XETH": "ETH", "ZUSD": "USD", "ZGBP": "GBP", "ZEUR": "EUR"}
GAP_THRESHOLD_US = 30_000_000  # 30 s without any update for a market is reported as a gap
MAX_REPORTED = 100


class ImportFormatError(ValueError):
    pass


def normalize_symbol(raw: str) -> str:
    """'BTC-USD' / 'btcusd' / 'XBT/USD' / 'XBTUSD' -> 'BTC/USD'."""
    s = raw.strip().upper()
    for sep in ("-", "/", "_"):
        if sep in s:
            b, q = s.split(sep, 1)
            return f"{ALIASES.get(b, b)}/{ALIASES.get(q, q)}"
    for q in QUOTES:
        if s.endswith(q) and len(s) > len(q):
            b = s[: -len(q)]
            return f"{ALIASES.get(b, b)}/{ALIASES.get(q, q)}"
    raise ImportFormatError(f"cannot split symbol {raw!r} into base/quote")


def us_to_dt(v: str) -> datetime:
    return datetime.fromtimestamp(int(v) / 1_000_000, tz=UTC)


def detect_format(header: list[str]) -> str:
    h = [c.strip() for c in header]
    if h[:4] != ["exchange", "symbol", "timestamp", "local_timestamp"]:
        raise ImportFormatError(f"unrecognised header (expected Tardis CSV layout): {h[:6]}")
    rest = h[4:]
    if rest == ["is_snapshot", "side", "price", "amount"]:
        return "incremental_book_L2"
    if rest == ["ask_amount", "ask_price", "bid_price", "bid_amount"]:
        return "quotes"
    if rest and rest[0] == "asks[0].price":
        return f"book_snapshot_{sum(1 for c in rest if c.startswith('asks[') and c.endswith('.price'))}"
    if rest[:4] == ["id", "side", "price", "amount"]:
        raise ImportFormatError("trades files contain executions, not order books; not importable as books")
    raise ImportFormatError(f"unsupported Tardis data type with columns {rest[:6]}")


def open_text(path: Path) -> io.TextIOBase:
    with open(path, "rb") as fh:
        magic = fh.read(2)
    if magic == b"\x1f\x8b":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", newline="")
    return open(path, encoding="utf-8", newline="")


def file_digest(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


# ============================================================================ book reconstruction
@dataclass
class BookState:
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    last_local_us: int = 0
    last_exchange_us: int = 0
    last_emit_us: int = 0
    in_snapshot: bool = False
    pending: list[int] | None = None  # [local_us, exchange_us] of an applied but not yet emitted message group

    def apply(self, side: str, price: Decimal, amount: Decimal) -> None:
        book = self.bids if side in ("bid", "buy") else self.asks
        if amount == 0:
            book.pop(price, None)
        else:
            book[price] = amount

    def levels(self, depth: int) -> tuple[list[list[str]], list[list[str]]]:
        b = sorted(self.bids.items(), key=lambda kv: kv[0], reverse=True)[:depth]
        a = sorted(self.asks.items(), key=lambda kv: kv[0])[:depth]
        return [[str(p), str(q)] for p, q in b], [[str(p), str(q)] for p, q in a]

    def to_json(self) -> dict:
        return {"bids": [[str(p), str(q)] for p, q in self.bids.items()],
                "asks": [[str(p), str(q)] for p, q in self.asks.items()],
                "last_local_us": self.last_local_us, "last_exchange_us": self.last_exchange_us,
                "last_emit_us": self.last_emit_us, "in_snapshot": self.in_snapshot, "pending": self.pending}

    @classmethod
    def from_json(cls, d: dict) -> BookState:
        return cls(bids={Decimal(p): Decimal(q) for p, q in d.get("bids", [])},
                   asks={Decimal(p): Decimal(q) for p, q in d.get("asks", [])},
                   last_local_us=d.get("last_local_us", 0), last_exchange_us=d.get("last_exchange_us", 0),
                   last_emit_us=d.get("last_emit_us", 0), in_snapshot=d.get("in_snapshot", False),
                   pending=d.get("pending"))


@dataclass
class Validation:
    rows: int = 0
    invalid_rows: int = 0
    duplicate_rows: int = 0
    out_of_order_local: int = 0
    out_of_order_exchange: int = 0
    snapshot_resets: int = 0
    emitted: int = 0
    sampled_out: int = 0
    crossed_emitted: int = 0
    empty_side_emitted: int = 0
    gaps: list[dict] = field(default_factory=list)
    gap_count: int = 0
    problems: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        if len(self.problems) < MAX_REPORTED:
            self.problems.append(msg)

    def to_json(self) -> dict:
        d = {k: getattr(self, k) for k in ("rows", "invalid_rows", "duplicate_rows", "out_of_order_local",
                                           "out_of_order_exchange", "snapshot_resets", "emitted", "sampled_out", "crossed_emitted",
                                           "empty_side_emitted", "gap_count")}
        d["gaps"] = self.gaps[:MAX_REPORTED]
        d["problems"] = self.problems[:MAX_REPORTED]
        return d

    @classmethod
    def from_json(cls, d: dict) -> Validation:
        v = cls()
        for k, val in (d or {}).items():
            if hasattr(v, k):
                setattr(v, k, val)
        return v


# ============================================================================ importer
@dataclass
class ImportResult:
    dataset_id: int
    status: str  # completed | skipped_duplicate | failed
    snapshots_written: int
    validation: dict
    resumed_from_line: int = 0
    message: str = ""


class HistoricalImporter:
    def __init__(self, db: Database, batch_rows: int = 5000, checkpoint_lines: int = 200_000):
        self.db = db
        self.batch_rows = batch_rows
        self.checkpoint_lines = checkpoint_lines

    async def import_file(self, path: str | Path, *, provider: str = "tardis.dev", source_url: str = "",
                          sample_interval_ms: int = 1000, depth: int = 25, notes: str = "",
                          stop_after_lines: int | None = None) -> ImportResult:
        """Import one file. ``stop_after_lines`` simulates an interruption (tests / chunked imports)."""
        path = Path(path)
        sha, size = file_digest(path)
        with open_text(path) as fh:
            header = next(csv.reader(fh), None)
        if not header:
            raise ImportFormatError("empty file")
        fmt = detect_format(header)

        async with self.db.session() as s:
            ds = (await s.execute(select(HistoricalDataset).where(
                HistoricalDataset.sha256 == sha, HistoricalDataset.sample_interval_ms == sample_interval_ms,
                HistoricalDataset.depth == depth))).scalar_one_or_none()
            if ds is not None and ds.status == "completed":
                return ImportResult(ds.id, "skipped_duplicate", ds.snapshots_written, ds.validation,
                                    message="identical file already imported")
            if ds is None:
                ds = HistoricalDataset(provider=provider, data_type=fmt, exchange="", symbol="", file_name=path.name,
                                       source_url=source_url, sha256=sha, size_bytes=size,
                                       sample_interval_ms=sample_interval_ms, depth=depth, status="importing",
                                       notes=notes, validation={})
                s.add(ds)
                await s.flush()
            ds.status = "importing"
            ds.error = ""
            dataset_id, start_line = ds.id, ds.checkpoint_line
            states = {k: BookState.from_json(v) for k, v in (ds.checkpoint_state or {}).items()}
            val = Validation.from_json(ds.validation)
            written = ds.snapshots_written

        try:
            result = await self._stream(path, fmt, dataset_id, start_line, states, val, written, sample_interval_ms,
                                        depth, stop_after_lines)
        except Exception as e:
            async with self.db.session() as s:
                d = await s.get(HistoricalDataset, dataset_id)
                d.status = "failed"
                d.error = f"{type(e).__name__}: {e}"[:4000]
            raise
        result.resumed_from_line = start_line
        return result

    async def _stream(self, path: Path, fmt: str, dataset_id: int, start_line: int, states: dict[str, BookState],
                      val: Validation, written: int, interval_ms: int, depth: int,
                      stop_after_lines: int | None) -> ImportResult:
        interval_us = interval_ms * 1000
        batch: list[dict] = []
        line_no = 0
        prev_row: list[str] | None = None
        meta: dict = {"exchange": None, "symbol": None, "raw_symbol": None, "first": None, "last": None}

        def key_of(ex: str, sym: str) -> str:
            return f"{ex}|{sym}"

        def emit(k: str, local_us: int, exch_us: int, bids: list, asks: list) -> None:
            ex, sym = k.split("|", 1)
            st = states[k]
            if st.last_emit_us and local_us - st.last_emit_us < interval_us:
                return
            prev = st.last_emit_us
            st.last_emit_us = local_us
            bb = Decimal(bids[0][0]) if bids else None
            ba = Decimal(asks[0][0]) if asks else None
            crossed = bb is not None and ba is not None and bb >= ba
            val.emitted += 1
            val.crossed_emitted += crossed
            val.empty_side_emitted += (not bids or not asks)
            batch.append({
                "exchange": ex, "symbol": sym, "source": DataSource.IMPORTED.value,
                "received_ts": us_to_dt(str(local_us)), "exchange_ts": us_to_dt(str(exch_us)), "sequence": None,
                "best_bid": bb, "best_ask": ba, "bids": bids, "asks": asks,
                "depth": max(len(bids), len(asks)), "dataset_id": dataset_id,
                "quality": {"crossed": crossed, "empty_side": not bids or not asks,
                            "gap_ms": round((local_us - prev) / 1000) if prev else None,
                            "provider_format": fmt},
            })

        def flush_pending(k: str) -> None:
            # incremental books are emitted once all rows sharing a local timestamp (one message) are applied
            st = states[k]
            if st.pending is not None:
                lu, eu = st.pending
                st.pending = None
                b, a = st.levels(depth)
                emit(k, lu, eu, b, a)

        async def commit(final: bool) -> None:
            nonlocal batch, written
            rows, batch = batch, []
            async with self.db.session() as s:
                if rows:
                    await s.execute(insert(OrderBookSnapshot), rows)
                d = await s.get(HistoricalDataset, dataset_id)
                written += len(rows)
                d.snapshots_written = written
                d.rows_read = val.rows
                d.checkpoint_line = line_no
                d.checkpoint_state = {k: st.to_json() for k, st in states.items()}
                d.validation = val.to_json()
                if meta["exchange"]:
                    d.exchange, d.symbol, d.raw_symbol = meta["exchange"], meta["symbol"], meta["raw_symbol"]
                if meta["first"] is not None and (d.first_exchange_ts is None or meta["first"] < d.first_exchange_ts):
                    d.first_exchange_ts = meta["first"]
                if meta["last"] is not None:
                    d.last_exchange_ts = max(meta["last"], d.last_exchange_ts or meta["last"])
                if final:
                    d.status = "completed"
                    d.completed_at = utcnow()

        with open_text(path) as fh:
            reader = csv.reader(fh)
            header = next(reader)
            book_cols = None
            if fmt.startswith("book_snapshot_"):
                n = int(fmt.rsplit("_", 1)[1])
                idx = {c: i for i, c in enumerate(header)}
                book_cols = ([(idx[f"bids[{i}].price"], idx[f"bids[{i}].amount"]) for i in range(n)],
                             [(idx[f"asks[{i}].price"], idx[f"asks[{i}].amount"]) for i in range(n)])
            for row in reader:
                line_no += 1
                if line_no <= start_line:
                    continue  # already consumed before the interruption
                if stop_after_lines is not None and line_no > start_line + stop_after_lines:
                    line_no -= 1
                    await commit(final=False)
                    return ImportResult(dataset_id, "interrupted", written, val.to_json(),
                                        message=f"stopped after line {line_no}")
                val.rows += 1
                if row == prev_row:
                    val.duplicate_rows += 1
                    continue
                prev_row = row
                try:
                    ex = row[0].strip().lower()
                    sym = normalize_symbol(row[1])
                    exch_us, local_us = int(row[2]), int(row[3] or row[2])
                except (IndexError, ValueError, ImportFormatError) as e:
                    val.invalid_rows += 1
                    val.note(f"line {line_no}: {e}")
                    continue
                k = key_of(ex, sym)
                if meta["exchange"] is None:
                    meta.update(exchange=ex, symbol=sym, raw_symbol=row[1])
                elif (meta["exchange"], meta["symbol"]) != (ex, sym) and meta["symbol"] != "MULTI":
                    meta.update(symbol="MULTI")
                st = states.setdefault(k, BookState())
                if st.last_local_us and local_us < st.last_local_us:
                    val.out_of_order_local += 1
                    val.note(f"line {line_no}: local timestamp went backwards for {k}")
                if st.last_exchange_us and exch_us < st.last_exchange_us:
                    val.out_of_order_exchange += 1
                if st.last_local_us and local_us - st.last_local_us > GAP_THRESHOLD_US:
                    val.gap_count += 1
                    if len(val.gaps) < MAX_REPORTED:
                        val.gaps.append({"market": k, "from": us_to_dt(str(st.last_local_us)).isoformat(),
                                         "to": us_to_dt(str(local_us)).isoformat(),
                                         "seconds": (local_us - st.last_local_us) / 1e6})
                ts = us_to_dt(str(exch_us))
                meta["first"] = ts if meta["first"] is None or ts < meta["first"] else meta["first"]
                meta["last"] = ts if meta["last"] is None or ts > meta["last"] else meta["last"]

                try:
                    if fmt == "incremental_book_L2":
                        if st.pending is not None and st.pending[0] != local_us:
                            flush_pending(k)
                        is_snap = row[4].strip().lower() == "true"
                        if is_snap and not st.in_snapshot:
                            if st.bids or st.asks:
                                val.snapshot_resets += 1
                            st.bids.clear()
                            st.asks.clear()
                        st.in_snapshot = is_snap
                        side, price, amount = row[5].strip().lower(), Decimal(row[6]), Decimal(row[7])
                        if side not in ("bid", "ask", "buy", "sell") or price <= 0 or amount < 0:
                            raise ValueError(f"invalid level {side} {price} {amount}")
                        st.apply(side, price, amount)
                        st.pending = [local_us, exch_us]  # a snapshot group emits once complete, like updates
                    elif st.last_emit_us and local_us - st.last_emit_us < interval_us:
                        # full-snapshot formats: this row would be dropped by the sampling interval, so its levels
                        # are not parsed (timestamp ordering and gaps above are still checked for every row)
                        val.sampled_out += 1
                    elif fmt == "quotes":
                        bids = [[row[6], row[7]]] if row[6] and row[7] else []
                        asks = [[row[5], row[4]]] if row[5] and row[4] else []
                        for p, q in bids + asks:
                            if Decimal(p) <= 0 or Decimal(q) < 0:
                                raise ValueError(f"invalid quote {p} {q}")
                        emit(k, local_us, exch_us, bids, asks)
                    else:
                        bid_cols, ask_cols = book_cols
                        bids = [[row[p], row[q]] for p, q in bid_cols if row[p] and row[q]]
                        asks = [[row[p], row[q]] for p, q in ask_cols if row[p] and row[q]]
                        for p, q in bids + asks:
                            if Decimal(p) <= 0 or Decimal(q) < 0:
                                raise ValueError(f"invalid level {p} {q}")
                        emit(k, local_us, exch_us, bids[:depth], asks[:depth])
                except (InvalidOperation, ValueError, IndexError) as e:
                    val.invalid_rows += 1
                    val.note(f"line {line_no}: {e}")
                    continue
                st.last_local_us = max(st.last_local_us, local_us)
                st.last_exchange_us = max(st.last_exchange_us, exch_us)
                if len(batch) >= self.batch_rows or line_no % self.checkpoint_lines == 0:
                    await commit(final=False)  # batch + checkpoint (incl. pending group state) in one transaction
        for k in list(states):
            flush_pending(k)
        await commit(final=True)
        return ImportResult(dataset_id, "completed", written, val.to_json())


def iter_files(paths: list[str]) -> Iterator[Path]:
    for p in paths:
        pp = Path(p)
        if pp.is_dir():
            yield from sorted(x for x in pp.iterdir() if x.suffix in (".csv", ".gz"))
        else:
            yield pp
