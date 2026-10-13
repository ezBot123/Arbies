"""Historical importer: format detection, L2 reconstruction, validation, provenance, resume and de-duplication.

Fixture files are small hand-written examples of the documented Tardis CSV layouts (test inputs only)."""

import gzip
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from arbies.core.models import DataSource
from arbies.db.models import HistoricalDataset, OrderBookSnapshot
from arbies.historical.importer import HistoricalImporter, ImportFormatError, detect_format, normalize_symbol

T0 = 1735689600_000000  # 2025-01-01T00:00:00Z in microseconds
L2_HEADER = "exchange,symbol,timestamp,local_timestamp,is_snapshot,side,price,amount"


def l2_lines():
    s = lambda sec: T0 + int(sec * 1_000_000)  # noqa: E731
    return [
        L2_HEADER,
        # initial snapshot (one message)
        f"coinbase,BTC-USD,{s(0)},{s(0.1)},true,bid,100.0,1",
        f"coinbase,BTC-USD,{s(0)},{s(0.1)},true,bid,99.0,2",
        f"coinbase,BTC-USD,{s(0)},{s(0.1)},true,ask,101.0,1",
        f"coinbase,BTC-USD,{s(0)},{s(0.1)},true,ask,102.0,3",
        # update 0.5 s later (same message: two rows) - throttled at 1 s sampling
        f"coinbase,BTC-USD,{s(0.5)},{s(0.6)},false,bid,100.5,1",
        f"coinbase,BTC-USD,{s(0.5)},{s(0.6)},false,ask,101.0,0",
        # 2 s: delete best bid
        f"coinbase,BTC-USD,{s(2)},{s(2.1)},false,bid,100.5,0",
        # 3 s: crossing update (bid above ask) - must be flagged, not repaired
        f"coinbase,BTC-USD,{s(3)},{s(3.1)},false,bid,103.0,1",
        # 3.05 s: local timestamp going backwards
        f"coinbase,BTC-USD,{s(3)},{s(3.05)},false,bid,103.0,0",
        # 40 s later: gap
        f"coinbase,BTC-USD,{s(45)},{s(45.1)},false,ask,101.5,1",
        f"coinbase,BTC-USD,{s(45)},{s(45.1)},false,ask,101.5,1",  # exact duplicate row
        "coinbase,BTC-USD,notanumber,1,false,bid,1,1",  # invalid row
    ]


def write(tmp_path, name, lines, gz=False):
    p = tmp_path / name
    data = ("\n".join(lines) + "\n").encode()
    p.write_bytes(gzip.compress(data) if gz else data)
    return p


async def rows(db):
    async with db.session() as s:
        return (await s.execute(select(OrderBookSnapshot).order_by(OrderBookSnapshot.received_ts))).scalars().all()


def test_symbol_normalisation_and_format_detection():
    assert normalize_symbol("BTC-USD") == "BTC/USD"
    assert normalize_symbol("btcusd") == "BTC/USD"
    assert normalize_symbol("XBT/USD") == "BTC/USD"
    assert normalize_symbol("ETHBTC") == "ETH/BTC"
    with pytest.raises(ImportFormatError):
        normalize_symbol("FOO")
    assert detect_format(L2_HEADER.split(",")) == "incremental_book_L2"
    assert detect_format("exchange,symbol,timestamp,local_timestamp,ask_amount,ask_price,bid_price,bid_amount".split(",")) == "quotes"
    snap = "exchange,symbol,timestamp,local_timestamp," + ",".join(
        f"asks[{i}].price,asks[{i}].amount,bids[{i}].price,bids[{i}].amount" for i in range(5))
    assert detect_format(snap.split(",")) == "book_snapshot_5"
    with pytest.raises(ImportFormatError):
        detect_format("exchange,symbol,timestamp,local_timestamp,id,side,price,amount".split(","))
    with pytest.raises(ImportFormatError):
        detect_format(["time", "price"])


async def test_incremental_l2_reconstruction_validation_and_provenance(db, tmp_path):
    p = write(tmp_path, "coinbase_incremental_book_L2_2025-01-01_BTC-USD.csv.gz", l2_lines(), gz=True)
    res = await HistoricalImporter(db).import_file(p, source_url="https://datasets.tardis.dev/v1/coinbase/...")
    assert res.status == "completed"
    out = await rows(db)
    assert all(r.source == DataSource.IMPORTED.value and r.dataset_id == res.dataset_id for r in out)
    # timestamps preserved: received = provider local time, exchange_ts = exchange time
    assert out[0].received_ts == datetime(2025, 1, 1, 0, 0, 0, 100000, tzinfo=UTC)
    assert out[0].exchange_ts == datetime(2025, 1, 1, tzinfo=UTC)
    # snapshot emitted complete; 0.6 s update throttled (1 s sampling); 2.1 s state reflects deletes
    assert out[0].bids == [["100.0", "1"], ["99.0", "2"]] and out[0].asks == [["101.0", "1"], ["102.0", "3"]]
    second = out[1]
    assert second.received_ts.timestamp() == pytest.approx(T0 / 1e6 + 2.1)
    assert second.bids[0] == ["100.0", "1"] and second.asks[0] == ["102.0", "3"]  # 101 ask deleted at 0.6 s
    crossed = [r for r in out if r.quality["crossed"]]
    assert len(crossed) == 1 and crossed[0].best_bid == D("103.0")  # flagged, not repaired
    v = res.validation
    assert v["out_of_order_local"] == 1 and v["duplicate_rows"] == 1 and v["invalid_rows"] == 1
    assert v["gap_count"] == 1 and v["gaps"][0]["seconds"] > 30 and v["crossed_emitted"] == 1
    async with db.session() as s:
        ds = await s.get(HistoricalDataset, res.dataset_id)
    assert ds.provider == "tardis.dev" and ds.exchange == "coinbase" and ds.symbol == "BTC/USD"
    assert ds.data_type == "incremental_book_L2" and len(ds.sha256) == 64 and ds.status == "completed"
    assert ds.first_exchange_ts == datetime(2025, 1, 1, tzinfo=UTC)


async def test_reimport_is_skipped_as_duplicate(db, tmp_path):
    p = write(tmp_path, "a.csv", l2_lines())
    imp = HistoricalImporter(db)
    first = await imp.import_file(p)
    again = await imp.import_file(p)
    assert again.status == "skipped_duplicate" and again.dataset_id == first.dataset_id
    async with db.session() as s:
        assert (await s.execute(select(func.count()).select_from(OrderBookSnapshot))).scalar_one() == first.snapshots_written


async def test_interrupted_import_resumes_to_identical_result(db, tmp_path):
    lines = [L2_HEADER]
    for i in range(300):  # 300 messages, one per 0.4 s, alternating sides
        ts = T0 + i * 400_000
        lines.append(f"kraken,XBT/USD,{ts},{ts + 1000},{'true' if i < 2 else 'false'},{'bid' if i % 2 else 'ask'},"
                     f"{100 + (i % 7) if i % 2 == 0 else 99 - (i % 5)},{1 + i % 3}")
    p = write(tmp_path, "k.csv", lines)
    imp = HistoricalImporter(db, batch_rows=7, checkpoint_lines=11)
    part = await imp.import_file(p, stop_after_lines=123)
    assert part.status == "interrupted"
    done = await imp.import_file(p)
    assert done.status == "completed" and done.resumed_from_line == 123
    resumed = [(r.received_ts, r.bids, r.asks) for r in await rows(db)]
    # reference: uninterrupted import of the same content into a clean database
    from arbies.db.session import Database
    ref_db = Database(f"sqlite+aiosqlite:///{tmp_path}/ref.db")
    await ref_db.create_all()
    await HistoricalImporter(ref_db).import_file(p)
    ref = [(r.received_ts, r.bids, r.asks) for r in await rows(ref_db)]
    await ref_db.dispose()
    assert resumed == ref and len(ref) > 50


async def test_book_snapshot_and_quotes_formats(db, tmp_path):
    hdr = "exchange,symbol,timestamp,local_timestamp," + ",".join(
        f"asks[{i}].price,asks[{i}].amount,bids[{i}].price,bids[{i}].amount" for i in range(2))
    snap = write(tmp_path, "snap.csv", [hdr, f"bitstamp,BTCUSD,{T0},{T0 + 5},101,1,100,2,102,1,99,3",
                                       f"bitstamp,BTCUSD,{T0 + 2_000_000},{T0 + 2_000_005},101,1,100,2,,,99,3"])
    q = write(tmp_path, "quotes.csv", ["exchange,symbol,timestamp,local_timestamp,ask_amount,ask_price,bid_price,bid_amount",
                                       f"kraken,XBT/GBP,{T0},{T0 + 9},0.5,80001,80000,0.7"])
    imp = HistoricalImporter(db)
    r1 = await imp.import_file(snap)
    r2 = await imp.import_file(q)
    assert r1.status == r2.status == "completed"
    out = {(r.exchange, r.symbol, r.received_ts): r for r in await rows(db)}
    b = [r for (e, s, _t), r in out.items() if e == "bitstamp"]
    assert b[0].bids == [["100", "2"], ["99", "3"]] and b[0].asks == [["101", "1"], ["102", "1"]]
    assert b[1].asks == [["101", "1"]]  # empty level fields are skipped, not invented
    k = [r for (e, s, _t), r in out.items() if e == "kraken"][0]
    assert k.symbol == "BTC/GBP" and k.bids == [["80000", "0.7"]] and k.asks == [["80001", "0.5"]] and k.depth == 1


async def test_retention_never_deletes_imported_history(db, tmp_path):
    from arbies.market_data.recorder import MarketDataRecorder

    p = write(tmp_path, "old.csv", l2_lines())  # 2025 data: far older than the retention window
    res = await HistoricalImporter(db).import_file(p)
    async with db.session() as s:
        s.add(OrderBookSnapshot(exchange="kraken", symbol="BTC/USD", source=DataSource.LIVE.value,
                                received_ts=datetime(2025, 1, 1, tzinfo=UTC), exchange_ts=None, bids=[], asks=[],
                                depth=0, quality={}))
    rec = MarketDataRecorder.__new__(MarketDataRecorder)
    rec.db, rec.retention_days = db, 30
    deleted = await rec.apply_retention()
    assert deleted == 1  # the old live-recorded row only
    async with db.session() as s:
        left = (await s.execute(select(func.count()).select_from(OrderBookSnapshot))).scalar_one()
    assert left == res.snapshots_written
