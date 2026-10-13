"""Reference prices: genuine exchange OHLC candles fetched from public exchange APIs.

Used ONLY for (a) valuing inventory and converting deposits when no order book is available,
(b) benchmark calculations and (c) clearly labelled candle screening. They are never fed to the
arbitrage detectors or the execution simulator: candles do not show executable bid/ask depth.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite

from arbies.db.models import ReferencePrice
from arbies.db.session import Database
from arbies.logging_setup import get_logger

log = get_logger(__name__)

INTERVAL_MS = {"1m": 60_000, "1h": 3_600_000, "1d": 86_400_000}
# Kraken's OHLC endpoint only serves the most recent 720 candles of any interval.
HISTORY_LIMITS = {("kraken", "1m"): "only the latest 720 minutes", ("kraken", "1h"): "only the latest 720 hours"}
PAGE = {"coinbase": 300, "bitstamp": 1000, "kraken": 720}


def _insert(db: Database):
    return postgresql.insert if db.engine.dialect.name == "postgresql" else sqlite.insert


async def store_candles(db: Database, exchange: str, symbol: str, interval: str, candles: list[list],
                        source_url: str = "") -> int:
    if not candles:
        return 0
    rows = [{"exchange": exchange, "symbol": symbol, "interval": interval,
             "ts": datetime.fromtimestamp(c[0] / 1000, UTC), "open": Decimal(str(c[1])), "high": Decimal(str(c[2])),
             "low": Decimal(str(c[3])), "close": Decimal(str(c[4])), "volume": Decimal(str(c[5] or 0)),
             "source_url": source_url} for c in candles if c[4] is not None]
    ins = _insert(db)
    n = 0
    async with db.session() as s:
        for i in range(0, len(rows), 2000):
            stmt = ins(ReferencePrice).values(rows[i:i + 2000]).on_conflict_do_nothing(
                index_elements=["exchange", "symbol", "interval", "ts"])
            res = await s.execute(stmt)
            n += res.rowcount or 0
    return n


async def fetch_range(db: Database, exchange: str, symbol: str, interval: str, start: datetime, end: datetime,
                      exchange_factory: Callable | None = None, progress: Callable[[float], None] | None = None) -> dict:
    """Fetch candles in [start, end) and store them. Resumes from the newest stored candle."""
    if (exchange, interval) in HISTORY_LIMITS and start < datetime.now(UTC) - timedelta(days=1):
        raise ValueError(f"{exchange} {interval} history unavailable: {HISTORY_LIMITS[(exchange, interval)]}")
    async with db.session() as s:
        newest = (await s.execute(select(func.max(ReferencePrice.ts)).where(
            ReferencePrice.exchange == exchange, ReferencePrice.symbol == symbol, ReferencePrice.interval == interval,
            ReferencePrice.ts >= start, ReferencePrice.ts < end))).scalar_one()
    since = int(((newest + timedelta(milliseconds=INTERVAL_MS[interval])) if newest else start).timestamp() * 1000)
    end_ms = int(min(end, datetime.now(UTC)).timestamp() * 1000)  # exchanges reject start times in the future
    if exchange_factory is None:
        import ccxt.async_support as ccxt_async

        from arbies.market_data.ccxt_config import ccxt_config

        def exchange_factory(ex_id):
            return getattr(ccxt_async, ex_id)(ccxt_config(enableRateLimit=True, timeout=30000))
    ex = exchange_factory(exchange)
    stored = fetched = 0
    page = PAGE.get(exchange, 500)
    try:
        while since < end_ms:
            for attempt in range(5):
                try:
                    batch = await ex.fetch_ohlcv(symbol, interval, since=since, limit=page)
                    break
                except Exception as e:  # noqa: BLE001 - network / rate limit: back off and retry
                    if attempt == 4:
                        raise
                    log.warning("candle fetch retry", exchange=exchange, symbol=symbol, error=str(e)[:200])
                    await asyncio.sleep(2 ** attempt)
            batch = [c for c in batch if since <= c[0] < end_ms]
            if not batch:
                # empty window (no trades / exchange gap): advance one page of time, never invent candles
                since += page * INTERVAL_MS[interval]
                continue
            fetched += len(batch)
            stored += await store_candles(db, exchange, symbol, interval, batch, f"{exchange} public OHLC")
            since = batch[-1][0] + INTERVAL_MS[interval]
            if progress:
                progress(min(1.0, (since - start.timestamp() * 1000) / max(1, end_ms - start.timestamp() * 1000)))
    finally:
        await ex.close()
    return {"exchange": exchange, "symbol": symbol, "interval": interval, "fetched": fetched, "stored": stored}


async def load_series(db: Database, exchange: str, symbol: str, interval: str, start: datetime,
                      end: datetime) -> list[ReferencePrice]:
    async with db.session() as s:
        return list((await s.execute(select(ReferencePrice).where(
            ReferencePrice.exchange == exchange, ReferencePrice.symbol == symbol, ReferencePrice.interval == interval,
            ReferencePrice.ts >= start, ReferencePrice.ts <= end).order_by(ReferencePrice.ts))).scalars().all())


async def reference_inventory(db: Database) -> list[dict]:
    async with db.session() as s:
        rows = (await s.execute(select(ReferencePrice.exchange, ReferencePrice.symbol, ReferencePrice.interval,
                                       func.count(), func.min(ReferencePrice.ts), func.max(ReferencePrice.ts))
                                .group_by(ReferencePrice.exchange, ReferencePrice.symbol, ReferencePrice.interval)
                                .order_by(ReferencePrice.exchange, ReferencePrice.symbol, ReferencePrice.interval))).all()
    return [{"exchange": e, "symbol": sym, "interval": iv, "candles": n, "first": a.isoformat(), "last": b.isoformat()}
            for e, sym, iv, n, a, b in rows]


class DailyMarks:
    """Daily reference prices keyed by date -> {symbol: (open, close)} from one preferred exchange
    with fallbacks. Lookups only return candles that had *opened* by the requested time; the close is
    only used once the day is over (no look-ahead)."""

    def __init__(self, series: dict[str, list[ReferencePrice]]):
        self.by_symbol: dict[str, dict] = {}
        for sym, rows in series.items():
            self.by_symbol[sym] = {r.ts.date(): r for r in rows}

    def price_at(self, symbol: str, when: datetime) -> tuple[Decimal | None, str]:
        """Latest price known at ``when``: the close of the most recent *completed* daily candle
        (a day's candle is only used once that day has ended - no look-ahead). Falls back up to 7 days."""
        rows = self.by_symbol.get(symbol, {})
        for back in range(0, 8):
            day = rows.get(when.date() - timedelta(days=back))
            if day is not None and when >= day.ts + timedelta(days=1):
                return day.close, f"{day.ts.date()} close"
        return None, "missing"

    def edges(self, when: datetime) -> dict[str, Decimal]:
        out = {}
        for sym in self.by_symbol:
            p, _ = self.price_at(sym, when)
            if p is not None:
                out[sym] = p
        return out
