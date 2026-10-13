"""Market data hub: owns adapters, keeps the latest normalised books and fans
updates out to subscribers (scanner, recorder, paper trader, websocket)."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from datetime import datetime

from arbies.config import MarketDataMode, Settings
from arbies.core.models import MarketRules, OrderBook, utcnow
from arbies.logging_setup import get_logger
from arbies.market_data.base import ConnStatus, ExchangeAdapter, ExchangeHealth

log = get_logger(__name__)

UK_ELIGIBILITY_NOTES = {
    "kraken": "Kraken offers spot trading to UK customers (at time of writing). Verify your account's eligibility, "
              "product availability and API permissions before relying on it.",
    "coinbase": "Coinbase offers spot trading to UK customers (at time of writing). Verify Advanced Trade API access "
                "for your account.",
    "bitstamp": "Bitstamp offers spot trading to UK customers (at time of writing). Verify API access for your account.",
    "gemini": "Gemini offers spot trading to UK customers (at time of writing). Verify API access for your account.",
}


class MarketDataHub:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.books: dict[tuple[str, str], OrderBook] = {}
        self.rules: dict[tuple[str, str], MarketRules] = {}
        self.adapters: dict[str, ExchangeAdapter] = {}
        self._subscribers: list[asyncio.Queue] = []
        self._event_listeners: list[Callable[[str, str | None, str, str], None]] = []
        self.events: deque[dict] = deque(maxlen=500)
        self._extra_tasks: list[asyncio.Task] = []
        self.mode = settings.market_data_mode
        self.started_at: datetime | None = None

    # ------------------------------------------------------------------ callbacks
    def on_book(self, ob: OrderBook) -> None:
        self.books[(ob.exchange, ob.symbol)] = ob
        for q in self._subscribers:
            if q.full():
                try:
                    q.get_nowait()  # drop oldest - consumers only care about fresh data
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(ob)

    def on_event(self, exchange: str, symbol: str | None, event: str, detail: str) -> None:
        ev = {"ts": utcnow().isoformat(), "exchange": exchange, "symbol": symbol, "event": event, "detail": detail}
        self.events.append(ev)
        for fn in self._event_listeners:
            try:
                fn(exchange, symbol, event, detail)
            except Exception:  # noqa: BLE001
                log.exception("event listener failed")
        if event in ("connected",):
            self._refresh_rules(exchange)

    def add_event_listener(self, fn) -> None:
        self._event_listeners.append(fn)

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _refresh_rules(self, exchange: str) -> None:
        ad = self.adapters.get(exchange)
        if ad is None:
            return
        for sym, r in ad.market_rules().items():
            self.rules[(exchange, sym)] = r

    # ------------------------------------------------------------------ lifecycle
    def add_adapter(self, adapter: ExchangeAdapter) -> None:
        self.adapters[adapter.exchange_id] = adapter

    def build_adapters(self) -> None:
        s = self.settings
        if s.market_data_mode == MarketDataMode.LIVE:
            from arbies.market_data.ccxt_adapter import CcxtAdapter
            for ex in s.exchanges:
                self.add_adapter(CcxtAdapter(ex, s.symbols, self.on_book, self.on_event, depth=s.orderbook_depth,
                                             rest_interval_s=s.rest_poll_interval_s))
        elif s.market_data_mode == MarketDataMode.SYNTHETIC:
            from arbies.market_data.synthetic import SyntheticAdapter, SyntheticWorld, world_ticker
            world = SyntheticWorld()
            self._extra_tasks.append(asyncio.create_task(world_ticker(world)))
            for ex in s.exchanges:
                self.add_adapter(SyntheticAdapter(ex, s.symbols, self.on_book, self.on_event, world,
                                                  depth=min(s.orderbook_depth, 20)))

    async def start(self) -> None:
        self.started_at = utcnow()
        if not self.adapters:
            self.build_adapters()
        for ad in self.adapters.values():
            await ad.start()
            self._refresh_rules(ad.exchange_id)

    async def stop(self) -> None:
        for ad in self.adapters.values():
            try:
                await ad.stop()
            except Exception:  # noqa: BLE001
                log.exception("adapter stop failed", exchange=ad.exchange_id)
        for t in self._extra_tasks:
            t.cancel()
        await asyncio.gather(*self._extra_tasks, return_exceptions=True)

    # ------------------------------------------------------------------ queries
    def health(self) -> dict[str, ExchangeHealth]:
        return {ex: ad.health for ex, ad in self.adapters.items()}

    def is_healthy(self, exchange: str, now: datetime | None = None) -> bool:
        ad = self.adapters.get(exchange)
        if ad is None:
            return False
        h = ad.health
        return h.status in (ConnStatus.CONNECTED, ConnStatus.DEGRADED) and h.fresh(
            now or utcnow(), self.settings.max_data_age_ms)

    def healthy_map(self, now: datetime | None = None) -> dict[str, bool]:
        now = now or utcnow()
        return {ex: self.is_healthy(ex, now) for ex in self.adapters}

    def fresh_books(self, now: datetime | None = None, max_age_ms: int | None = None) -> dict[tuple[str, str], OrderBook]:
        now = now or utcnow()
        limit = max_age_ms if max_age_ms is not None else self.settings.max_data_age_ms * 4
        return {k: ob for k, ob in self.books.items() if ob.age_ms(now) <= limit}

    def data_source(self) -> str:
        return {"live": "live_recorded", "synthetic": "synthetic", "off": "none"}[self.mode.value]
