"""Asset valuation in the reporting currency.

Conversion rates are derived from observed order-book mid prices (a graph search
over available markets). Only if no market path exists is a configured static
fallback rate used, and such valuations are flagged ``estimated=True``.
Mid prices are acceptable for *valuation*; they are never used for opportunity
profitability, which always uses executable depth-walked prices.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from arbies.core.models import OrderBook, split_symbol


@dataclass(frozen=True, slots=True)
class Rate:
    value: Decimal
    estimated: bool  # True when a static fallback rate was used


class Valuator:
    def __init__(self, reporting: str = "GBP", fallback: dict[str, Decimal] | None = None):
        self.reporting = reporting
        self.fallback = dict(fallback or {})
        self._edges: dict[str, dict[str, Decimal]] = {}
        # Optional reference prices (e.g. completed daily candle closes) used only for pairs that no
        # order book provides. Set by long-horizon simulations; empty everywhere else.
        self.base_prices: dict[str, Decimal] = {}

    def update_from_books(self, books: Iterable[OrderBook]) -> None:
        edges: dict[str, dict[str, Decimal]] = {}
        acc: dict[tuple[str, str], list[Decimal]] = {}
        for ob in books:
            mid = ob.mid
            if mid is None or mid <= 0:
                continue
            base, quote = split_symbol(ob.symbol)
            acc.setdefault((base, quote), []).append(mid)
        for (base, quote), mids in acc.items():
            m = sum(mids) / len(mids)
            edges.setdefault(base, {})[quote] = m
            edges.setdefault(quote, {})[base] = 1 / m
        for sym, p in self.base_prices.items():
            base, quote = split_symbol(sym)
            if p > 0 and quote not in edges.get(base, {}):
                edges.setdefault(base, {})[quote] = p
                edges.setdefault(quote, {})[base] = 1 / p
        self._edges = edges

    def set_edges(self, prices: dict[str, Decimal]) -> None:
        """Set direct prices, e.g. {"BTC/GBP": Decimal(50000)} (testing / replay)."""
        edges: dict[str, dict[str, Decimal]] = {}
        for sym, p in prices.items():
            base, quote = split_symbol(sym)
            edges.setdefault(base, {})[quote] = p
            edges.setdefault(quote, {})[base] = 1 / p
        self._edges = edges

    def rate(self, asset: str, target: str | None = None) -> Rate | None:
        target = target or self.reporting
        if asset == target:
            return Rate(Decimal(1), False)
        # BFS for shortest market path
        seen = {asset}
        q: deque[tuple[str, Decimal]] = deque([(asset, Decimal(1))])
        while q:
            cur, r = q.popleft()
            for nxt, px in self._edges.get(cur, {}).items():
                if nxt in seen:
                    continue
                nr = r * px
                if nxt == target:
                    return Rate(nr, False)
                seen.add(nxt)
                q.append((nxt, nr))
        if target == self.reporting:
            if asset in self.fallback:
                return Rate(self.fallback[asset], True)
            # try asset -> X via market, X -> reporting via fallback
            for fx_asset, fx_rate in self.fallback.items():
                r = self.rate(asset, fx_asset) if fx_asset != asset else None
                if r is not None and not r.estimated:
                    return Rate(r.value * fx_rate, True)
        return None

    def value(self, asset: str, amount: Decimal) -> tuple[Decimal | None, bool]:
        r = self.rate(asset)
        if r is None:
            return None, True
        return amount * r.value, r.estimated

    def to_reporting(self, asset: str, amount: Decimal) -> Decimal:
        v, _ = self.value(asset, amount)
        return v if v is not None else Decimal(0)

    def from_reporting(self, asset: str, amount: Decimal) -> Decimal | None:
        r = self.rate(asset)
        if r is None or r.value == 0:
            return None
        return amount / r.value
