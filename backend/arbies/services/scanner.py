"""Continuous opportunity scanner for the dashboard.

Runs the deterministic detectors on the latest books, keeps the current
opportunity table in memory, and records opportunity *episodes* (first
appearance and qualification changes) to the database for later analysis.
No LLM is involved anywhere in this path.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import insert

from arbies.arbitrage.cross_exchange import CrossExchangeDetector
from arbies.arbitrage.models import Opportunity, StrategyParams
from arbies.arbitrage.triangular import TriangularDetector
from arbies.config import Settings
from arbies.core.models import utcnow
from arbies.core.valuation import Valuator
from arbies.db.models import OpportunityRecord
from arbies.db.session import Database
from arbies.logging_setup import get_logger
from arbies.market_data.hub import MarketDataHub

log = get_logger(__name__)


def opp_record(o: Opportunity) -> dict:
    return {
        "key": o.key, "kind": o.kind.value, "detected_at": o.detected_at, "first_seen": o.first_seen or o.detected_at,
        "exchanges": o.exchanges, "symbols": o.symbols, "route": o.route, "profit_currency": o.profit_currency,
        "gross_spread_pct": o.gross_spread_pct, "gross_profit": o.gross_profit, "fees": o.fees,
        "total_costs": o.total_costs, "net_profit": o.net_profit, "net_profit_gbp": o.net_profit_gbp,
        "net_return_pct": o.net_return_pct, "notional_gbp": o.notional_gbp, "size_base": o.size_base,
        "max_size_base": o.max_size_base, "data_age_ms": o.data_age_ms, "confidence": o.confidence,
        "qualified": o.qualified, "rejection_reasons": [r.value for r in o.rejection_reasons],
        "data_source": o.data_source, "detail": o.to_dict(),
    }


class Scanner:
    def __init__(self, hub: MarketDataHub, db: Database | None, settings: Settings,
                 params: StrategyParams | None = None,
                 balance_provider: Callable[[], Callable[[str, str], Decimal | None] | None] | None = None):
        self.hub = hub
        self.db = db
        self.settings = settings
        self.params = params or StrategyParams(min_net_profit_pct=settings.min_net_profit_pct,
                                               max_trade_gbp=settings.max_trade_size_gbp,
                                               max_data_age_ms=settings.max_data_age_ms)
        self.valuator = Valuator(settings.reporting_currency, settings.fallback_fx_to_gbp)
        self.balance_provider = balance_provider
        self.enabled = {"cross_exchange": True, "triangular": True}
        self.current: list[Opportunity] = []
        self.first_seen: dict[str, datetime] = {}
        self._last_state: dict[str, bool] = {}  # key -> qualified (for episode recording)
        self._pending_records: list[dict] = []
        self.stats = {"scans": 0, "last_scan_ms": 0.0, "avg_scan_ms": 0.0, "recorded": 0, "detected_episodes": 0,
                      "qualified_episodes": 0}
        self.recent_qualified: deque[dict] = deque(maxlen=200)
        self._task: asyncio.Task | None = None
        self._listeners: list[Callable[[list[Opportunity]], None]] = []

    def set_params(self, params: StrategyParams) -> None:
        self.params = params

    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def scan(self, now: datetime | None = None) -> list[Opportunity]:
        now = now or utcnow()
        t0 = time.perf_counter()
        books = self.hub.fresh_books(now)
        self.valuator.update_from_books(books.values())
        rules = self.hub.rules
        balances = self.balance_provider() if self.balance_provider else None
        healthy = self.hub.healthy_map(now)
        opps: list[Opportunity] = []
        if self.enabled["cross_exchange"]:
            opps += CrossExchangeDetector(self.params, rules, self.valuator).evaluate(
                books, now, balances, lambda e: healthy.get(e, False))
        if self.enabled["triangular"]:
            opps += TriangularDetector(self.params, rules, self.valuator).evaluate(
                books, now, balances, lambda e: healthy.get(e, False))
        live = set()
        for o in opps:
            live.add(o.key)
            if o.gross_spread_pct > 0:
                o.first_seen = self.first_seen.setdefault(o.key, now)
                prev = self._last_state.get(o.key)
                if prev is None or prev != o.qualified:
                    self._pending_records.append(opp_record(o))
                    if prev is None:
                        self.stats["detected_episodes"] += 1
                    if o.qualified:
                        self.stats["qualified_episodes"] += 1
                        self.recent_qualified.appendleft(o.to_dict(now))
                self._last_state[o.key] = o.qualified
            else:
                self.first_seen.pop(o.key, None)
                self._last_state.pop(o.key, None)
        for k in list(self.first_seen):
            if k not in live:
                self.first_seen.pop(k, None)
                self._last_state.pop(k, None)
        opps.sort(key=lambda o: (o.qualified, o.net_return_pct), reverse=True)
        self.current = opps
        dt = (time.perf_counter() - t0) * 1000
        self.stats["scans"] += 1
        self.stats["last_scan_ms"] = round(dt, 3)
        self.stats["avg_scan_ms"] = round(dt if self.stats["scans"] == 1 else 0.95 * self.stats["avg_scan_ms"] + 0.05 * dt, 3)
        for fn in self._listeners:
            try:
                fn(opps)
            except Exception:  # noqa: BLE001
                log.exception("scanner listener failed")
        return opps

    async def flush_records(self) -> None:
        if not self.db or not self._pending_records:
            return
        rows, self._pending_records = self._pending_records[:2000], self._pending_records[2000:]
        try:
            async with self.db.session() as s:
                await s.execute(insert(OpportunityRecord), rows)
            self.stats["recorded"] += len(rows)
        except Exception as e:  # noqa: BLE001
            log.error("opportunity record flush failed", error=str(e))

    async def run(self) -> None:
        interval = self.settings.scan_interval_ms / 1000
        last_flush = time.monotonic()
        while True:
            try:
                self.scan()
            except Exception:  # noqa: BLE001
                log.exception("scan failed")
            if time.monotonic() - last_flush > 2:
                await self.flush_records()
                last_flush = time.monotonic()
            await asyncio.sleep(interval)

    async def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="scanner")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self.flush_records()
