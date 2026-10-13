"""Public market data via CCXT (REST) and CCXT Pro (WebSocket).

No credentials are used here - only public endpoints. CCXT's built-in rate
limiter is enabled. WebSocket streams are preferred; after repeated WebSocket
failures a symbol falls back to REST polling at ``rest_poll_interval_s``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal

import ccxt
import ccxt.async_support  # noqa: F401 - REST fallback classes
import ccxt.pro as ccxtpro

from arbies.core.models import D, DataSource, MarketRules, OrderBook, utcnow
from arbies.logging_setup import get_logger
from arbies.market_data.base import BookCallback, ConnStatus, EventCallback, ExchangeAdapter, ExchangeHealth
from arbies.market_data.ccxt_config import ccxt_config

log = get_logger(__name__)

# Some exchanges only accept specific depth values on their order-book streams.
WS_LIMITS: dict[str, int | None] = {"kraken": 25, "bitstamp": None, "coinbase": None, "gemini": None}
REST_LIMITS: dict[str, int | None] = {"kraken": 25, "bitstamp": None, "coinbase": 50, "gemini": 25}
WS_FAILURES_BEFORE_REST = 3
# A WebSocket diff-book that stays crossed this long has drifted from the exchange (missed deletions):
# drop it and resubscribe so CCXT rebuilds it from a fresh snapshot. Brief crossings are left alone.
CROSSED_RESYNC_S = 5.0
# A symbol that keeps drifting despite resubscribing is polled over REST instead.
RESYNCS_BEFORE_REST = 3
RESYNC_WINDOW_S = 120.0


def _step_from_precision(value, mode: int) -> Decimal | None:
    if value is None:
        return None
    if mode == ccxt.DECIMAL_PLACES:
        return Decimal(1).scaleb(-int(value))
    if mode == ccxt.TICK_SIZE:
        return D(value)
    return None  # SIGNIFICANT_DIGITS: not representable as a fixed step


class CcxtAdapter(ExchangeAdapter):
    def __init__(self, exchange_id: str, symbols: list[str], on_book: BookCallback, on_event: EventCallback,
                 depth: int = 25, use_websocket: bool = True, rest_interval_s: float = 2.0,
                 exchange_factory=None):
        self.exchange_id = exchange_id
        self.requested_symbols = symbols
        self.on_book = on_book
        self.on_event = on_event
        self.depth = depth
        self.use_websocket = use_websocket
        self.rest_interval_s = rest_interval_s
        self.health = ExchangeHealth(exchange_id, transport="websocket" if use_websocket else "rest")
        self._factory = exchange_factory
        self._exchange = None
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._rules: dict[str, MarketRules] = {}
        self._rest_mode: set[str] = set()
        self._crossed_since: dict[str, float] = {}
        self._resync_times: dict[str, list[float]] = {}

    # ------------------------------------------------------------------ lifecycle
    def _create(self):
        if self._factory is not None:
            return self._factory(self.exchange_id)
        cls = getattr(ccxtpro, self.exchange_id, None) if self.use_websocket else None
        if cls is None:
            cls = getattr(ccxt.async_support, self.exchange_id)
            self.use_websocket = False
            self.health.transport = "rest"
        return cls(ccxt_config(enableRateLimit=True, newUpdates=False, timeout=15000))

    async def start(self) -> None:
        self._running = True
        self._tasks.append(asyncio.create_task(self._bootstrap(), name=f"md-{self.exchange_id}-bootstrap"))

    async def _bootstrap(self) -> None:
        backoff = 2.0
        while self._running:
            try:
                self._exchange = self._create()
                await self._exchange.load_markets()
                break
            except Exception as e:  # noqa: BLE001
                self._error(None, f"load_markets failed: {type(e).__name__}: {e}")
                self.health.status = ConnStatus.ERROR
                await self._close_exchange()
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        if not self._running:
            return
        ex = self._exchange
        available = [s for s in self.requested_symbols if s in ex.markets and ex.markets[s].get("active", True) is not False]
        self.health.symbols = available
        self.health.unavailable_symbols = [s for s in self.requested_symbols if s not in available]
        for s in available:
            self._rules[s] = self._rules_from_market(ex.markets[s], ex.precisionMode)
        await self._sync_clock()
        self.health.status = ConnStatus.CONNECTED
        self.health.connected_since = utcnow()
        self.on_event(self.exchange_id, None, "connected", f"{len(available)} symbols ({self.health.transport})")
        for s in available:
            self._tasks.append(asyncio.create_task(self._symbol_loop(s), name=f"md-{self.exchange_id}-{s}"))
        self._tasks.append(asyncio.create_task(self._clock_loop(), name=f"md-{self.exchange_id}-clock"))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self._close_exchange()
        self.health.status = ConnStatus.DISCONNECTED
        self.on_event(self.exchange_id, None, "disconnected", "stopped")

    async def _close_exchange(self) -> None:
        if self._exchange is not None:
            try:
                await self._exchange.close()
            except Exception:  # noqa: BLE001
                pass
            self._exchange = None

    # ------------------------------------------------------------------ rules / clock
    def _rules_from_market(self, m: dict, mode: int) -> MarketRules:
        limits = m.get("limits") or {}
        prec = m.get("precision") or {}
        return MarketRules(
            exchange=self.exchange_id,
            symbol=m["symbol"],
            base=m["base"],
            quote=m["quote"],
            taker_fee=D(m.get("taker")) if m.get("taker") is not None else Decimal("0.0026"),
            maker_fee=D(m.get("maker")) if m.get("maker") is not None else Decimal("0.0016"),
            amount_step=_step_from_precision(prec.get("amount"), mode),
            price_step=_step_from_precision(prec.get("price"), mode),
            min_amount=D((limits.get("amount") or {}).get("min")) if (limits.get("amount") or {}).get("min") else None,
            min_cost=D((limits.get("cost") or {}).get("min")) if (limits.get("cost") or {}).get("min") else None,
            active=m.get("active", True) is not False,
            # base-tier rate from CCXT's bundled fee table, not fetched from the exchange (Kraken's public API no
            # longer publishes fee tiers). It can lag exchange changes; account-specific tiers need credentials.
            fee_source="ccxt_default_schedule" if m.get("taker") is not None else "default",
        )

    def market_rules(self) -> dict[str, MarketRules]:
        return dict(self._rules)

    async def _sync_clock(self) -> None:
        ex = self._exchange
        if ex is None or not ex.has.get("fetchTime"):
            return
        try:
            t0 = time.time() * 1000
            server = await ex.fetch_time()
            t1 = time.time() * 1000
            if server:
                self.health.clock_offset_ms = float(server) - (t0 + t1) / 2
        except Exception as e:  # noqa: BLE001
            log.debug("clock sync failed", exchange=self.exchange_id, error=str(e))

    async def _clock_loop(self) -> None:
        while self._running:
            await asyncio.sleep(300)
            await self._sync_clock()

    # ------------------------------------------------------------------ streaming
    def _error(self, symbol: str | None, msg: str) -> None:
        self.health.errors += 1
        self.health.last_error = msg[:500]
        self.health.last_error_at = utcnow()
        log.warning("market data error", exchange=self.exchange_id, symbol=symbol, error=msg[:200])
        self.on_event(self.exchange_id, symbol, "error", msg[:500])

    def _emit(self, symbol: str, raw: dict) -> None:
        now = utcnow()
        ex_ts = None
        if raw.get("timestamp"):
            ex_ts = datetime.fromtimestamp(raw["timestamp"] / 1000, tz=UTC)
            lat = (now - ex_ts).total_seconds() * 1000
            h = self.health
            h.latency_ms_ewma = lat if h.latency_ms_ewma is None else 0.9 * h.latency_ms_ewma + 0.1 * lat
        ob = OrderBook.from_levels(self.exchange_id, symbol, raw.get("bids", [])[: self.depth],
                                   raw.get("asks", [])[: self.depth], received_ts=now, exchange_ts=ex_ts,
                                   source=DataSource.LIVE, sequence=raw.get("nonce"))
        self.health.last_update[symbol] = now
        self.health.updates += 1
        self.on_book(ob)

    @staticmethod
    def _raw_crossed(raw: dict) -> bool:
        bids, asks = raw.get("bids") or [], raw.get("asks") or []
        return bool(bids and asks and bids[0][0] >= asks[0][0])

    async def _resync_if_stuck_crossed(self, ex, symbol: str, raw: dict) -> bool:
        """Resubscribe a WebSocket book that has stayed crossed for CROSSED_RESYNC_S. Returns True if resynced."""
        if not self._raw_crossed(raw):
            self._crossed_since.pop(symbol, None)
            return False
        since = self._crossed_since.setdefault(symbol, time.monotonic())
        if time.monotonic() - since < CROSSED_RESYNC_S:
            return False
        self._crossed_since.pop(symbol, None)
        now = time.monotonic()
        recent = [t for t in self._resync_times.get(symbol, []) if now - t < RESYNC_WINDOW_S] + [now]
        self._resync_times[symbol] = recent
        self.health.resyncs += 1
        msg = f"book crossed for >{CROSSED_RESYNC_S:.0f}s (bid {raw['bids'][0][0]} >= ask {raw['asks'][0][0]}); resubscribing"
        log.warning("order book resync", exchange=self.exchange_id, symbol=symbol, detail=msg)
        self.on_event(self.exchange_id, symbol, "resync", msg)
        if len(recent) < RESYNCS_BEFORE_REST and ex.has.get("unWatchOrderBook"):
            try:
                await asyncio.wait_for(ex.un_watch_order_book(symbol), timeout=10)
                return True
            except Exception as e:  # noqa: BLE001
                self._error(symbol, f"unWatchOrderBook failed: {type(e).__name__}: {e}")
        # cannot resubscribe cleanly, or it keeps drifting: poll this symbol over REST instead
        if ex.has.get("unWatchOrderBook"):
            try:
                await asyncio.wait_for(ex.un_watch_order_book(symbol), timeout=10)
            except Exception:  # noqa: BLE001 - best effort; REST polling does not depend on it
                pass
        self._rest_mode.add(symbol)
        self.health.transport = "websocket+rest"
        self.on_event(self.exchange_id, symbol, "fallback_rest",
                      f"WebSocket book kept crossing ({len(recent)} resyncs in {RESYNC_WINDOW_S:.0f}s); switched to REST polling")
        return True

    async def _symbol_loop(self, symbol: str) -> None:
        ws_failures = 0
        backoff = 1.0
        while self._running:
            ex = self._exchange
            try:
                if self.use_websocket and symbol not in self._rest_mode and ex.has.get("watchOrderBook"):
                    raw = await ex.watch_order_book(symbol, WS_LIMITS.get(self.exchange_id))
                    ws_failures = 0
                    if await self._resync_if_stuck_crossed(ex, symbol, raw):
                        continue
                else:
                    raw = await ex.fetch_order_book(symbol, REST_LIMITS.get(self.exchange_id))
                    await asyncio.sleep(self.rest_interval_s)
                self._emit(symbol, raw)
                backoff = 1.0
                if self.health.status != ConnStatus.CONNECTED and not self._rest_mode:
                    self.health.status = ConnStatus.CONNECTED
            except asyncio.CancelledError:
                raise
            except (ccxt.BadSymbol, ccxt.NotSupported) as e:
                self._error(symbol, f"symbol unsupported: {e}")
                if symbol in self.health.symbols:
                    self.health.symbols.remove(symbol)
                self.health.unavailable_symbols.append(symbol)
                return
            except Exception as e:  # noqa: BLE001 - network errors, rate limits, disconnects
                self._error(symbol, f"{type(e).__name__}: {e}")
                self.health.reconnects += 1
                self.on_event(self.exchange_id, symbol, "reconnecting", f"backoff {backoff:.0f}s")
                if self.use_websocket and symbol not in self._rest_mode:
                    ws_failures += 1
                    if ws_failures >= WS_FAILURES_BEFORE_REST:
                        self._rest_mode.add(symbol)
                        self.health.status = ConnStatus.DEGRADED
                        self.health.transport = "websocket+rest"
                        self.on_event(self.exchange_id, symbol, "fallback_rest", "switched to REST polling")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
