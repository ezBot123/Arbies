"""Historical dataset loading from recorded order-book snapshots.

A dataset is identified by a content hash so that a backtest result can be tied
to exactly the data it used. Data provenance (live recorded / imported /
synthetic) is part of the query - sources are never mixed silently.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from arbies.core.models import DataSource, OrderBook
from arbies.db.models import OrderBookSnapshot
from arbies.db.session import Database


def row_to_book(r: OrderBookSnapshot | tuple) -> OrderBook:
    return OrderBook.from_levels(r.exchange, r.symbol, r.bids, r.asks, received_ts=r.received_ts,
                                 exchange_ts=r.exchange_ts, source=DataSource(r.source), sequence=r.sequence)


async def describe_available(session: AsyncSession) -> list[dict]:
    """Inventory of recorded data by source and market."""
    q = (select(OrderBookSnapshot.source, OrderBookSnapshot.exchange, OrderBookSnapshot.symbol,
                func.count(), func.min(OrderBookSnapshot.received_ts), func.max(OrderBookSnapshot.received_ts))
         .group_by(OrderBookSnapshot.source, OrderBookSnapshot.exchange, OrderBookSnapshot.symbol)
         .order_by(OrderBookSnapshot.source, OrderBookSnapshot.exchange, OrderBookSnapshot.symbol))
    rows = (await session.execute(q)).all()
    return [{"source": s, "exchange": e, "symbol": sym, "snapshots": n,
             "first": a.isoformat() if a else None, "last": b.isoformat() if b else None}
            for s, e, sym, n, a, b in rows]


async def dataset_summary(db: Database, source: DataSource, exchanges: list[str], symbols: list[str],
                          start: datetime, end: datetime) -> dict:
    async with db.session() as s:
        base = (OrderBookSnapshot.source == source.value, OrderBookSnapshot.exchange.in_(exchanges),
                OrderBookSnapshot.symbol.in_(symbols), OrderBookSnapshot.received_ts >= start,
                OrderBookSnapshot.received_ts <= end)
        q = select(func.count(), func.min(OrderBookSnapshot.id), func.max(OrderBookSnapshot.id),
                   func.sum(OrderBookSnapshot.id), func.min(OrderBookSnapshot.received_ts),
                   func.max(OrderBookSnapshot.received_ts)).where(*base)
        n, min_id, max_id, sum_id, first, last = (await s.execute(q)).one()
        per = (await s.execute(select(OrderBookSnapshot.exchange, OrderBookSnapshot.symbol, func.count())
                               .where(*base).group_by(OrderBookSnapshot.exchange, OrderBookSnapshot.symbol))).all()
    ident = f"{source.value}|{sorted(exchanges)}|{sorted(symbols)}|{start.isoformat()}|{end.isoformat()}|{n}|{min_id}|{max_id}|{sum_id}"
    return {
        "dataset_id": hashlib.sha256(ident.encode()).hexdigest()[:32],
        "source": source.value,
        "snapshots": int(n or 0),
        "first": first.isoformat() if first else None,
        "last": last.isoformat() if last else None,
        "per_market": {f"{e} {sym}": int(c) for e, sym, c in per},
    }


async def load_books(db: Database, source: DataSource, exchanges: list[str], symbols: list[str],
                     start: datetime, end: datetime, chunk: int = 20000) -> list[OrderBook]:
    """Load books ordered by (received_ts, id). Uses keyset pagination to bound memory per query."""
    out: list[OrderBook] = []
    last_ts: datetime | None = None
    last_id = 0
    async with db.session() as s:
        while True:
            q = select(OrderBookSnapshot).where(
                OrderBookSnapshot.source == source.value,
                OrderBookSnapshot.exchange.in_(exchanges),
                OrderBookSnapshot.symbol.in_(symbols),
                OrderBookSnapshot.received_ts >= start,
                OrderBookSnapshot.received_ts <= end,
            )
            if last_ts is not None:
                q = q.where((OrderBookSnapshot.received_ts > last_ts)
                            | ((OrderBookSnapshot.received_ts == last_ts) & (OrderBookSnapshot.id > last_id)))
            q = q.order_by(OrderBookSnapshot.received_ts, OrderBookSnapshot.id).limit(chunk)
            rows = (await s.execute(q)).scalars().all()
            if not rows:
                break
            out.extend(row_to_book(r) for r in rows)
            last_ts, last_id = rows[-1].received_ts, rows[-1].id
            if len(rows) < chunk:
                break
    return out


def find_gaps(books: list[OrderBook], threshold: timedelta = timedelta(seconds=30)) -> list[dict]:
    """Per-market gaps longer than ``threshold`` (data-quality indicator)."""
    last: dict[tuple[str, str], datetime] = {}
    gaps = []
    for ob in books:
        k = (ob.exchange, ob.symbol)
        prev = last.get(k)
        if prev is not None and ob.received_ts - prev > threshold:
            gaps.append({"exchange": ob.exchange, "symbol": ob.symbol, "from": prev.isoformat(),
                         "to": ob.received_ts.isoformat(), "seconds": (ob.received_ts - prev).total_seconds()})
        last[k] = ob.received_ts
    return gaps


def iter_window(books: list[OrderBook], start: datetime, end: datetime) -> Iterator[OrderBook]:
    for ob in books:
        if start <= ob.received_ts <= end:
            yield ob
