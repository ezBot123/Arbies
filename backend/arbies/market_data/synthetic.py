"""Synthetic market data generator.

FOR DEVELOPMENT, DEMOS AND TESTS ONLY. Every book produced here carries
``DataSource.SYNTHETIC`` and the dashboard labels it prominently. It is never
used as a silent substitute for real exchange data: it only runs when
``ARBIES_MARKET_DATA_MODE=synthetic`` is set explicitly.

Model: a shared seeded random walk for BTC/USD, ETH/USD and GBP/USD, with
per-exchange price noise, varying depth, and occasional short-lived
dislocations so the arbitrage pipeline can be exercised end-to-end. The
frequency and size of dislocations are arbitrary and say nothing about real
markets.
"""

from __future__ import annotations

import asyncio
import math
import random
import zlib
from datetime import datetime, timedelta
from decimal import Decimal

from arbies.core.models import D, DataSource, MarketRules, OrderBook, split_symbol, utcnow
from arbies.market_data.base import BookCallback, ConnStatus, EventCallback, ExchangeAdapter, ExchangeHealth

# indicative starting levels (synthetic)
BASE_USD = {"BTC": 60000.0, "ETH": 3000.0, "USDC": 1.0, "USDT": 1.0, "SOL": 150.0, "USD": 1.0}
GBPUSD = 1.27
EURUSD = 1.08
FEES = {"kraken": ("0.0040", "0.0025"), "coinbase": ("0.0060", "0.0040"), "bitstamp": ("0.0040", "0.0030")}


class SyntheticWorld:
    """Shared latent prices so that all synthetic exchanges are correlated."""

    def __init__(self, seed: int = 1234):
        self.rng = random.Random(seed)
        self.usd = dict(BASE_USD)
        self.gbpusd = GBPUSD
        self.eurusd = EURUSD

    def step(self, dt_s: float) -> None:
        vol = {"BTC": 0.5, "ETH": 0.65, "SOL": 0.9}  # annualised-ish, scaled below
        for a, v in vol.items():
            if a in self.usd:
                self.usd[a] *= math.exp(self.rng.gauss(0, v * math.sqrt(dt_s / 31_536_000)) * 1.0)
        self.gbpusd *= math.exp(self.rng.gauss(0, 0.08 * math.sqrt(dt_s / 31_536_000)))

    def price(self, base: str, quote: str) -> float | None:
        def usd(a):
            if a == "GBP":
                return self.gbpusd
            if a == "EUR":
                return self.eurusd
            return self.usd.get(a)
        b, q = usd(base), usd(quote)
        if b is None or q is None:
            return None
        return b / q


class SyntheticAdapter(ExchangeAdapter):
    def __init__(self, exchange_id: str, symbols: list[str], on_book: BookCallback, on_event: EventCallback,
                 world: SyntheticWorld, depth: int = 20, interval_s: float = 0.5, seed: int = 0,
                 dislocation_prob: float = 0.01):
        self.exchange_id = exchange_id
        self.symbols = [s for s in symbols if world.price(*split_symbol(s)) is not None]
        self.on_book = on_book
        self.on_event = on_event
        self.world = world
        self.depth = depth
        self.interval_s = interval_s
        self.rng = random.Random(zlib.crc32(f"{exchange_id}:{seed}".encode()))
        self.dislocation_prob = dislocation_prob
        self.health = ExchangeHealth(exchange_id, transport="synthetic", symbols=list(self.symbols))
        self._task: asyncio.Task | None = None
        self._bias: dict[str, float] = {s: self.rng.gauss(0, 0.0004) for s in self.symbols}
        self._disloc: dict[str, tuple[float, datetime]] = {}
        taker, maker = FEES.get(exchange_id, ("0.0040", "0.0025"))
        self._rules = {
            s: MarketRules(exchange_id, s, *split_symbol(s), taker_fee=Decimal(taker), maker_fee=Decimal(maker),
                           amount_step=Decimal("0.00000001"), price_step=Decimal("0.01") if split_symbol(s)[1] in ("USD", "GBP", "EUR") else Decimal("0.00001"),
                           min_amount=Decimal("0.00001"), min_cost=Decimal("0.5"), fee_source="synthetic")
            for s in self.symbols
        }

    def market_rules(self) -> dict[str, MarketRules]:
        return dict(self._rules)

    def make_book(self, symbol: str, now: datetime) -> OrderBook:
        base, quote = split_symbol(symbol)
        fair = self.world.price(base, quote)
        assert fair is not None
        # slowly drifting per-exchange bias
        self._bias[symbol] = 0.98 * self._bias[symbol] + self.rng.gauss(0, 0.0001)
        mid = fair * (1 + self._bias[symbol])
        d = self._disloc.get(symbol)
        if d and d[1] > now:
            mid *= 1 + d[0]
        elif self.rng.random() < self.dislocation_prob:
            shift = self.rng.choice([-1, 1]) * self.rng.uniform(0.003, 0.012)
            self._disloc[symbol] = (shift, now + timedelta(seconds=self.rng.uniform(1, 6)))
            mid *= 1 + shift
        half_spread = mid * self.rng.uniform(0.00005, 0.0004)
        tick = Decimal(1).scaleb(math.floor(math.log10(mid)) - 5)
        bids, asks = [], []
        notional_scale = 2000 / max(self.world.price(base, "USD") or 1.0, 1e-9)  # ~$2k per level
        for i in range(self.depth):
            step = mid * 0.0002 * (i + self.rng.random())
            size = notional_scale * self.rng.uniform(0.1, 3.0) * (1 + i * 0.3)
            bp = mid - half_spread - step
            ap = mid + half_spread + step
            bids.append(((D(f"{bp:.12f}") / tick).quantize(Decimal(1)) * tick, D(f"{size:.8f}")))
            asks.append(((D(f"{ap:.12f}") / tick).quantize(Decimal(1)) * tick, D(f"{size:.8f}")))
        return OrderBook.from_levels(self.exchange_id, symbol, bids, asks, received_ts=now, exchange_ts=now,
                                     source=DataSource.SYNTHETIC)

    async def start(self) -> None:
        self.health.status = ConnStatus.CONNECTED
        self.health.connected_since = utcnow()
        self.on_event(self.exchange_id, None, "connected", "SYNTHETIC data generator")
        self._task = asyncio.create_task(self._run(), name=f"synthetic-{self.exchange_id}")

    async def _run(self) -> None:
        while True:
            now = utcnow()
            for s in self.symbols:
                ob = self.make_book(s, now)
                self.health.last_update[s] = now
                self.health.updates += 1
                self.on_book(ob)
            await asyncio.sleep(self.interval_s * self.rng.uniform(0.8, 1.2))

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.health.status = ConnStatus.DISCONNECTED


async def world_ticker(world: SyntheticWorld, interval_s: float = 0.5) -> None:
    while True:
        world.step(interval_s)
        await asyncio.sleep(interval_s)


def generate_history(exchanges: list[str], symbols: list[str], start: datetime, end: datetime,
                     interval_ms: int = 1000, seed: int = 1234, depth: int = 10,
                     dislocation_prob: float = 0.01) -> list[OrderBook]:
    """Generate an in-memory synthetic history (for tests and demo datasets).

    Exchange snapshots are offset by random sub-interval jitter so timestamps
    interleave realistically. All books are labelled SYNTHETIC.
    """
    world = SyntheticWorld(seed)
    sinks: list[OrderBook] = []
    adapters = [SyntheticAdapter(ex, symbols, sinks.append, lambda *a: None, world, depth=depth, seed=seed,
                                 dislocation_prob=dislocation_prob) for ex in exchanges]
    t = start
    step = timedelta(milliseconds=interval_ms)
    jitter_rng = random.Random(seed + 1)
    out: list[OrderBook] = []
    while t < end:
        world.step(interval_ms / 1000)
        for ad in adapters:
            for s in ad.symbols:
                jt = t + timedelta(milliseconds=jitter_rng.uniform(0, interval_ms * 0.9))
                out.append(ad.make_book(s, jt))
        t += step
    out.sort(key=lambda ob: ob.received_ts)
    return out
