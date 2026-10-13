"""Single-exchange triangular arbitrage detection.

A cycle converts a start currency through two intermediate currencies and back,
e.g. USD -> BTC -> ETH -> USD, using three markets on one exchange. Each hop is
simulated sequentially against order-book depth, with lot-size rounding,
minimum-order checks and per-leg taker fees.

Fee model: in triangular routes the taker fee is deducted from the currency
received on each leg (as most exchanges do for chained conversions).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from arbies.arbitrage.depth import fill_by_base, fill_by_quote
from arbies.arbitrage.models import (
    BalanceFn,
    HealthFn,
    Leg,
    Opportunity,
    RejectionReason,
    StrategyKind,
    StrategyParams,
    confidence_from_age,
)
from arbies.core.models import ZERO, MarketRules, OrderBook, Side, split_symbol
from arbies.core.valuation import Valuator

BPS = Decimal("10000")
DEFAULT_TAKER = Decimal("0.0026")


@dataclass(frozen=True, slots=True)
class Hop:
    symbol: str
    side: Side  # BUY: spend quote, receive base. SELL: spend base, receive quote.
    src: str
    dst: str


Cycle = tuple[Hop, Hop, Hop]


def discover_cycles(symbols: list[str], start_currencies: list[str]) -> list[Cycle]:
    """Discover all valid 3-hop cycles beginning and ending in a start currency."""
    edges: dict[str, list[Hop]] = {}
    for sym in symbols:
        base, quote = split_symbol(sym)
        edges.setdefault(quote, []).append(Hop(sym, Side.BUY, quote, base))
        edges.setdefault(base, []).append(Hop(sym, Side.SELL, base, quote))
    cycles: list[Cycle] = []
    for start in start_currencies:
        for h1 in edges.get(start, []):
            for h2 in edges.get(h1.dst, []):
                if h2.dst in (start, h1.src) or h2.symbol == h1.symbol:
                    continue
                for h3 in edges.get(h2.dst, []):
                    if h3.dst == start and h3.symbol not in (h1.symbol, h2.symbol):
                        cycles.append((h1, h2, h3))
    return cycles


@dataclass(slots=True)
class HopResult:
    hop: Hop
    spent: Decimal
    received: Decimal  # after fee
    fee: Decimal
    base_amount: Decimal
    quote_amount: Decimal
    vwap: Decimal
    top_price: Decimal
    levels_used: int
    filled: bool
    meets_min: bool


def simulate_cycle(cycle: Cycle, start_amount: Decimal, books: dict[str, OrderBook],
                   rules: dict[str, MarketRules], fees: dict[str, Decimal], apply_fees: bool = True
                   ) -> tuple[Decimal, list[HopResult], Decimal]:
    """Simulate the cycle. Returns (final amount in start currency, hop results,
    start amount actually consumed)."""
    amount = start_amount
    consumed = start_amount
    results: list[HopResult] = []
    for i, hop in enumerate(cycle):
        ob = books[hop.symbol]
        r = rules.get(hop.symbol)
        fee_rate = fees.get(hop.symbol, DEFAULT_TAKER) if apply_fees else ZERO
        if hop.side == Side.BUY:
            est = fill_by_quote(ob.asks, amount)
            base_amt = r.round_amount(est.base_amount) if r else est.base_amount
            exact = fill_by_base(ob.asks, base_amt, is_buy=True)
            spent = exact.quote_amount
            gross_recv = base_amt
            filled = est.fully_filled and exact.fully_filled
            top = ob.asks[0].price if ob.asks else ZERO
            vwap = exact.vwap or top
            meets = r.meets_minimums(base_amt, vwap) if r else base_amt > 0
            quote_amt = spent
        else:
            base_amt = r.round_amount(amount) if r else amount
            est = fill_by_base(ob.bids, base_amt, is_buy=False)
            spent = base_amt
            gross_recv = est.quote_amount
            filled = est.fully_filled
            top = ob.bids[0].price if ob.bids else ZERO
            vwap = est.vwap or top
            meets = r.meets_minimums(base_amt, vwap) if r else base_amt > 0
            quote_amt = gross_recv
        if i == 0:
            consumed = spent  # unspent dust from lot rounding stays in the start currency
        fee = gross_recv * fee_rate
        received = gross_recv - fee
        results.append(HopResult(hop, spent, received, fee, base_amt, quote_amt, vwap, top,
                                 est.levels_used, filled, meets))
        amount = received
    return amount, results, consumed


class TriangularDetector:
    def __init__(self, params: StrategyParams, rules: dict[tuple[str, str], MarketRules] | None = None,
                 valuator: Valuator | None = None):
        self.params = params
        self.rules = rules or {}
        self.valuator = valuator or Valuator()
        self._cycle_cache: dict[str, tuple[frozenset[str], list[Cycle]]] = {}

    def cycles_for(self, exchange: str, symbols: list[str]) -> list[Cycle]:
        key = frozenset(symbols)
        cached = self._cycle_cache.get(exchange)
        if cached and cached[0] == key:
            return cached[1]
        cycles = discover_cycles(sorted(symbols), self.params.start_currencies)
        self._cycle_cache[exchange] = (key, cycles)
        return cycles

    def evaluate(self, books: dict[tuple[str, str], OrderBook], now: datetime,
                 balances: BalanceFn | None = None, health: HealthFn | None = None) -> list[Opportunity]:
        by_ex: dict[str, dict[str, OrderBook]] = {}
        for (ex, sym), ob in books.items():
            if ob.asks and ob.bids:
                by_ex.setdefault(ex, {})[sym] = ob
        out = []
        for ex, ex_books in by_ex.items():
            for cycle in self.cycles_for(ex, list(ex_books)):
                out.append(self.evaluate_cycle(ex, cycle, ex_books, now, balances, health))
        return out

    def evaluate_cycle(self, exchange: str, cycle: Cycle, books: dict[str, OrderBook], now: datetime,
                       balances: BalanceFn | None = None, health: HealthFn | None = None) -> Opportunity:
        p = self.params
        start = cycle[0].src
        route = [start, cycle[0].dst, cycle[1].dst, start]
        rules = {h.symbol: self.rules[(exchange, h.symbol)] for h in cycle if (exchange, h.symbol) in self.rules}
        fees = {}
        for h in cycle:
            r = self.rules.get((exchange, h.symbol))
            fees[h.symbol] = p.taker_fee(exchange, h.symbol, r.taker_fee if r else DEFAULT_TAKER)
        involved = [books[h.symbol] for h in cycle]
        age = max(ob.age_ms(now) for ob in involved)
        confidence = confidence_from_age(age, p.max_data_age_ms)
        if any(ob.is_crossed() for ob in involved):
            confidence = 0.0

        top_rate = Decimal(1)
        for h in cycle:
            ob = books[h.symbol]
            top_rate *= (1 / ob.asks[0].price) if h.side == Side.BUY else ob.bids[0].price

        opp = Opportunity(
            kind=StrategyKind.TRIANGULAR,
            key=Opportunity.make_key(StrategyKind.TRIANGULAR, [exchange, "->".join(route)]),
            exchanges=[exchange],
            symbols=[h.symbol for h in cycle],
            route=route,
            detected_at=now,
            profit_currency=start,
            gross_spread_pct=(top_rate - 1) * 100,
            data_age_ms=age,
            confidence=confidence,
            data_source=involved[0].source.value if len({ob.source for ob in involved}) == 1 else "mixed",
        )
        if age > p.max_data_age_ms:
            opp.reject(RejectionReason.STALE_DATA)
        if health is not None and not health(exchange):
            opp.reject(RejectionReason.EXCHANGE_UNAVAILABLE)
        if top_rate <= 1:
            opp.reject(RejectionReason.SPREAD_NON_POSITIVE)
            return opp

        max_start = self.valuator.from_reporting(start, p.max_trade_gbp)
        if max_start is None:
            opp.reject(RejectionReason.NO_VALUATION)
            return opp
        cap = max_start
        if balances is not None:
            bal = balances(exchange, start)
            if bal is not None:
                if bal <= 0:
                    opp.reject(RejectionReason.INSUFFICIENT_INVENTORY)
                else:
                    cap = min(cap, bal)
        opp.max_size_base = cap

        best = None
        steps = max(1, p.triangular_size_steps)
        for k in range(1, steps + 1):
            size = cap * k / steps
            final_gross, _, consumed_g = simulate_cycle(cycle, size, books, rules, fees, apply_fees=False)
            final_net, hops, consumed = simulate_cycle(cycle, size, books, rules, fees, apply_fees=True)
            if not all(h.filled for h in hops):
                continue
            gross = final_gross - consumed_g
            fee_total = final_gross - final_net
            n_legs = Decimal(len(cycle))
            slip = consumed * p.slippage_buffer_bps * n_legs / BPS
            latency = consumed * p.latency_cost_bps * n_legs / BPS
            legfail = consumed * p.leg_failure_prob * p.leg_failure_loss_bps / BPS
            net = final_net - consumed - slip - latency - legfail
            cand = dict(size=consumed, gross=gross, fees=fee_total, slip=slip, latency=latency,
                        legfail=legfail, net=net, hops=hops, meets=all(h.meets_min for h in hops))
            if best is None or cand["net"] > best["net"]:
                best = cand
        if best is None or best["size"] <= 0:
            opp.reject(RejectionReason.INSUFFICIENT_LIQUIDITY)
            return opp

        opp.size_base = best["size"]
        opp.notional = best["size"]
        opp.gross_profit = best["gross"]
        opp.fees = best["fees"]
        opp.slippage_buffer = best["slip"]
        opp.latency_cost = best["latency"]
        opp.leg_failure_cost = best["legfail"]
        opp.net_profit = best["net"]
        opp.net_return_pct = best["net"] / best["size"] * 100
        depth_slip = ZERO
        for h in best["hops"]:
            if h.top_price > 0 and h.base_amount > 0:
                diff = abs(h.vwap - h.top_price) * h.base_amount
                # express in start currency approximately via valuator
                q = split_symbol(h.hop.symbol)[1]
                conv = self.valuator.rate(q, start)
                depth_slip += diff * (conv.value if conv else 0)
            opp.legs.append(Leg(exchange, h.hop.symbol, h.hop.side, h.base_amount, h.vwap, h.quote_amount,
                                h.fee, h.hop.dst, h.top_price, h.levels_used))
        opp.depth_slippage = depth_slip
        opp.net_profit_gbp, _ = self.valuator.value(start, opp.net_profit)
        opp.notional_gbp, _ = self.valuator.value(start, opp.notional)

        if not best["meets"]:
            opp.reject(RejectionReason.BELOW_MIN_ORDER)
        if opp.gross_profit <= 0:
            opp.reject(RejectionReason.INSUFFICIENT_LIQUIDITY)
        elif opp.net_profit <= 0:
            opp.reject(RejectionReason.FEES_EXCEED_PROFIT)
        elif opp.net_return_pct < p.min_net_profit_pct:
            opp.reject(RejectionReason.BELOW_THRESHOLD)
        opp.qualified = not opp.rejection_reasons
        return opp
