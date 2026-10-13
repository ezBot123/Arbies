"""Order-book depth calculations.

These functions walk order-book levels to compute *executable* volume-weighted
prices, never top-of-book or mid prices. They are pure and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arbies.core.models import ZERO, BookLevel


@dataclass(frozen=True, slots=True)
class FillEstimate:
    base_amount: Decimal  # base currency filled
    quote_amount: Decimal  # quote currency spent (buy) or received (sell), before fees
    levels_used: int
    fully_filled: bool
    worst_price: Decimal | None

    @property
    def vwap(self) -> Decimal | None:
        if self.base_amount == 0:
            return None
        return self.quote_amount / self.base_amount


def fill_by_base(levels: list[BookLevel], base_amount: Decimal, limit_price: Decimal | None = None,
                 is_buy: bool = True) -> FillEstimate:
    """Fill ``base_amount`` against ``levels`` (asks for a buy, bids for a sell).

    If ``limit_price`` is given, levels beyond it are not consumed (IOC semantics),
    which can produce a partial fill.
    """
    remaining = base_amount
    quote = ZERO
    used = 0
    worst = None
    for lv in levels:
        if remaining <= 0:
            break
        if limit_price is not None:
            if is_buy and lv.price > limit_price:
                break
            if not is_buy and lv.price < limit_price:
                break
        take = min(remaining, lv.amount)
        quote += take * lv.price
        remaining -= take
        used += 1
        worst = lv.price
    filled = base_amount - remaining
    return FillEstimate(filled, quote, used, remaining <= 0, worst)


def fill_by_quote(levels: list[BookLevel], quote_amount: Decimal, limit_price: Decimal | None = None) -> FillEstimate:
    """Spend ``quote_amount`` buying base from ``levels`` (asks)."""
    remaining = quote_amount
    base = ZERO
    used = 0
    worst = None
    for lv in levels:
        if remaining <= 0:
            break
        if limit_price is not None and lv.price > limit_price:
            break
        level_cost = lv.price * lv.amount
        if level_cost <= remaining:
            base += lv.amount
            remaining -= level_cost
        else:
            base += remaining / lv.price
            remaining = ZERO
        used += 1
        worst = lv.price
    spent = quote_amount - remaining
    return FillEstimate(base, spent, used, remaining <= 0, worst)


def cumulative_amounts(levels: list[BookLevel]) -> list[Decimal]:
    out = []
    total = ZERO
    for lv in levels:
        total += lv.amount
        out.append(total)
    return out


def total_depth(levels: list[BookLevel]) -> Decimal:
    return sum((lv.amount for lv in levels), ZERO)


def depth_within(levels: list[BookLevel], reference: Decimal, pct: Decimal, is_ask: bool) -> Decimal:
    """Base liquidity within ``pct`` percent of ``reference`` price."""
    if is_ask:
        bound = reference * (1 + pct / 100)
        return sum((lv.amount for lv in levels if lv.price <= bound), ZERO)
    bound = reference * (1 - pct / 100)
    return sum((lv.amount for lv in levels if lv.price >= bound), ZERO)


def max_crossing_amount(asks: list[BookLevel], bids: list[BookLevel]) -> Decimal:
    """Base amount for which buying on ``asks`` and selling on ``bids`` is still
    marginally profitable before fees (i.e. while ask price < bid price)."""
    i = j = 0
    a_rem = asks[0].amount if asks else ZERO
    b_rem = bids[0].amount if bids else ZERO
    total = ZERO
    while i < len(asks) and j < len(bids) and asks[i].price < bids[j].price:
        take = min(a_rem, b_rem)
        total += take
        a_rem -= take
        b_rem -= take
        if a_rem <= 0:
            i += 1
            a_rem = asks[i].amount if i < len(asks) else ZERO
        if b_rem <= 0:
            j += 1
            b_rem = bids[j].amount if j < len(bids) else ZERO
    return total
