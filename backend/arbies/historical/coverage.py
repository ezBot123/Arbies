"""Order-book data coverage by source, exchange, pair, day and month.

"Covered" is measured in whole minutes containing at least one order-book snapshot. For cross-exchange
arbitrage the relevant measure is *overlap*: minutes in which at least two exchanges have books for the same
pair. Coverage is reported, never assumed: an uncovered minute is a minute the simulator could not trade.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import func, select

from arbies.core.models import DataSource
from arbies.db.models import HistoricalDataset, OrderBookSnapshot
from arbies.db.session import Database

ORDER_BOOK_SOURCES = (DataSource.LIVE.value, DataSource.IMPORTED.value)


def _minute_expr(db: Database):
    if db.engine.dialect.name == "postgresql":
        return func.date_trunc("minute", OrderBookSnapshot.received_ts)
    return func.strftime("%Y-%m-%d %H:%M", OrderBookSnapshot.received_ts)


def _to_minute(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return datetime.strptime(str(v), "%Y-%m-%d %H:%M").replace(tzinfo=UTC)


async def minute_sets(db: Database, start: datetime, end: datetime, exchanges: list[str] | None = None,
                      symbols: list[str] | None = None, sources: tuple[str, ...] = ORDER_BOOK_SOURCES
                      ) -> dict[tuple[str, str, str], set[datetime]]:
    m = _minute_expr(db)
    q = (select(OrderBookSnapshot.source, OrderBookSnapshot.exchange, OrderBookSnapshot.symbol, m)
         .where(OrderBookSnapshot.received_ts >= start, OrderBookSnapshot.received_ts <= end,
                OrderBookSnapshot.source.in_(sources))
         .group_by(OrderBookSnapshot.source, OrderBookSnapshot.exchange, OrderBookSnapshot.symbol, m))
    if exchanges:
        q = q.where(OrderBookSnapshot.exchange.in_(exchanges))
    if symbols:
        q = q.where(OrderBookSnapshot.symbol.in_(symbols))
    out: dict[tuple[str, str, str], set[datetime]] = defaultdict(set)
    async with db.session() as s:
        for src, ex, sym, minute in (await s.execute(q)).all():
            out[(src, ex, sym)].add(_to_minute(minute))
    return out


def month_iter(start: datetime, end: datetime):
    y, mo = start.year, start.month
    while (y, mo) <= (end.year, end.month):
        yield y, mo
        y, mo = (y + 1, 1) if mo == 12 else (y, mo + 1)


def minutes_in_month(y: int, mo: int, start: datetime, end: datetime) -> int:
    first = max(start, datetime(y, mo, 1, tzinfo=UTC))
    nxt = datetime(y + (mo == 12), 1 if mo == 12 else mo + 1, 1, tzinfo=UTC)
    last = min(end, nxt)
    return max(0, int((last - first).total_seconds() // 60))


async def coverage_report(db: Database, start: datetime, end: datetime, exchanges: list[str] | None = None,
                          symbols: list[str] | None = None) -> dict:
    sets = await minute_sets(db, start, end, exchanges, symbols)
    total_minutes = int((end - start).total_seconds() // 60)
    markets = []
    by_day: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    per_symbol: dict[str, dict[str, set[datetime]]] = defaultdict(lambda: defaultdict(set))
    for (src, ex, sym), mins in sorted(sets.items()):
        days = defaultdict(int)
        for mm in mins:
            days[mm.date().isoformat()] += 1
            by_day[mm.date().isoformat()][f"{ex} {sym}"] += 1
        per_symbol[sym][ex] |= mins
        markets.append({"source": src, "exchange": ex, "symbol": sym, "covered_minutes": len(mins),
                        "covered_pct": round(100 * len(mins) / total_minutes, 4) if total_minutes else 0,
                        "days_with_data": len(days),
                        "first": min(mins).isoformat(), "last": max(mins).isoformat()})
    overlap: set[datetime] = set()
    overlap_by_symbol = {}
    for sym, by_ex in per_symbol.items():
        counts: dict[datetime, int] = defaultdict(int)
        for mins in by_ex.values():
            for mm in mins:
                counts[mm] += 1
        o = {mm for mm, c in counts.items() if c >= 2}
        overlap_by_symbol[sym] = len(o)
        overlap |= o
    months = []
    for y, mo in month_iter(start, end):
        total = minutes_in_month(y, mo, start, end)
        cov = sum(1 for mm in overlap if mm.year == y and mm.month == mo)
        months.append({"month": f"{y}-{mo:02d}", "minutes": total, "overlap_minutes": cov,
                       "overlap_pct": round(100 * cov / total, 4) if total else 0.0})
    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(), "minutes": total_minutes},
        "markets": markets,
        "overlap_minutes": len(overlap),
        "overlap_pct": round(100 * len(overlap) / total_minutes, 4) if total_minutes else 0.0,
        "overlap_by_symbol": overlap_by_symbol,
        "months": months,
        "days": {d: dict(v) for d, v in sorted(by_day.items())},
        "continuous": bool(total_minutes) and len(overlap) >= total_minutes * 0.99,
        "definition": "A minute is covered when it contains at least one order-book snapshot; overlap requires "
                      ">= 2 exchanges with books for the same pair in that minute.",
    }


async def dataset_list(db: Database) -> list[dict]:
    async with db.session() as s:
        rows = (await s.execute(select(HistoricalDataset).order_by(HistoricalDataset.id.desc()))).scalars().all()
    return [{"id": d.id, "provider": d.provider, "data_type": d.data_type, "exchange": d.exchange, "symbol": d.symbol,
             "raw_symbol": d.raw_symbol, "file_name": d.file_name, "source_url": d.source_url, "sha256": d.sha256,
             "size_bytes": d.size_bytes, "status": d.status, "rows_read": d.rows_read,
             "snapshots_written": d.snapshots_written, "sample_interval_ms": d.sample_interval_ms, "depth": d.depth,
             "first_exchange_ts": d.first_exchange_ts.isoformat() if d.first_exchange_ts else None,
             "last_exchange_ts": d.last_exchange_ts.isoformat() if d.last_exchange_ts else None,
             "validation": d.validation, "error": d.error, "notes": d.notes,
             "created_at": d.created_at.isoformat() if d.created_at else None,
             "completed_at": d.completed_at.isoformat() if d.completed_at else None} for d in rows]

