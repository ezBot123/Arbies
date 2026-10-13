"""Persists order-book snapshots and connection events for replay.

Runs outside the critical scanning path: it consumes from its own hub queue,
throttles per market and bulk-inserts in batches.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import delete, insert

from arbies.core.models import OrderBook, utcnow
from arbies.db.models import ConnectionEvent, OrderBookSnapshot
from arbies.db.session import Database
from arbies.logging_setup import get_logger
from arbies.market_data.hub import MarketDataHub

log = get_logger(__name__)


def snapshot_row(ob: OrderBook, depth: int, prev_ts: datetime | None) -> dict:
    return {
        "exchange": ob.exchange,
        "symbol": ob.symbol,
        "source": ob.source.value,
        "received_ts": ob.received_ts,
        "exchange_ts": ob.exchange_ts,
        "sequence": ob.sequence,
        "best_bid": ob.best_bid,
        "best_ask": ob.best_ask,
        "bids": [[str(lv.price), str(lv.amount)] for lv in ob.bids[:depth]],
        "asks": [[str(lv.price), str(lv.amount)] for lv in ob.asks[:depth]],
        "depth": min(depth, max(len(ob.bids), len(ob.asks))),
        "quality": {
            "crossed": ob.is_crossed(),
            "gap_ms": round((ob.received_ts - prev_ts).total_seconds() * 1000) if prev_ts else None,
            "empty_side": not ob.bids or not ob.asks,
        },
    }


class MarketDataRecorder:
    def __init__(self, hub: MarketDataHub, db: Database, interval_ms: int = 1000, depth: int = 20,
                 retention_days: int = 30, flush_interval_s: float = 1.0):
        self.hub = hub
        self.db = db
        self.interval = timedelta(milliseconds=interval_ms)
        self.depth = depth
        self.retention_days = retention_days
        self.flush_interval_s = flush_interval_s
        self._last: dict[tuple[str, str], datetime] = {}
        self._buffer: list[dict] = []
        self._events: list[dict] = []
        self._tasks: list[asyncio.Task] = []
        self.rows_written = 0
        self.write_errors = 0
        self.last_flush: datetime | None = None

    def _on_event(self, exchange, symbol, event, detail) -> None:
        self._events.append({"ts": utcnow(), "exchange": exchange, "symbol": symbol, "event": event,
                             "detail": detail[:1000]})

    async def start(self) -> None:
        self._queue = self.hub.subscribe(maxsize=5000)
        self.hub.add_event_listener(self._on_event)
        self._tasks = [asyncio.create_task(self._consume()), asyncio.create_task(self._flusher()),
                       asyncio.create_task(self._retention_loop())]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self.hub.unsubscribe(self._queue)
        await self.flush()

    def offer(self, ob: OrderBook) -> bool:
        key = (ob.exchange, ob.symbol)
        prev = self._last.get(key)
        if prev is not None and ob.received_ts - prev < self.interval:
            return False
        self._buffer.append(snapshot_row(ob, self.depth, prev))
        self._last[key] = ob.received_ts
        return True

    async def _consume(self) -> None:
        while True:
            ob = await self._queue.get()
            self.offer(ob)

    async def _flusher(self) -> None:
        while True:
            await asyncio.sleep(self.flush_interval_s)
            await self.flush()

    async def flush(self) -> None:
        rows, self._buffer = self._buffer, []
        events, self._events = self._events, []
        if not rows and not events:
            return
        try:
            async with self.db.session() as s:
                if rows:
                    await s.execute(insert(OrderBookSnapshot), rows)
                if events:
                    await s.execute(insert(ConnectionEvent), events)
            self.rows_written += len(rows)
            self.last_flush = utcnow()
        except Exception as e:  # noqa: BLE001
            self.write_errors += 1
            log.error("recorder flush failed", error=str(e), rows=len(rows))

    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            await self.apply_retention()

    async def apply_retention(self) -> int:
        cutoff = utcnow() - timedelta(days=self.retention_days)
        async with self.db.session() as s:
            res = await s.execute(delete(OrderBookSnapshot).where(OrderBookSnapshot.received_ts < cutoff))
            return res.rowcount or 0

    def status(self) -> dict:
        return {
            "rows_written": self.rows_written,
            "write_errors": self.write_errors,
            "buffered": len(self._buffer),
            "last_flush": self.last_flush.isoformat() if self.last_flush else None,
            "interval_ms": self.interval.total_seconds() * 1000,
            "depth": self.depth,
            "retention_days": self.retention_days,
        }
