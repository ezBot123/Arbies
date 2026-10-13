"""Cross-exchange spot arbitrage detection.

Buy the base asset on exchange A (walking A's asks) and simultaneously sell the
same amount on exchange B (walking B's bids). Requires pre-positioned inventory:
quote currency on A and base currency on B. No transfers happen per trade; the
cost of periodically rebalancing inventory is amortised via
``rebalancing_cost_bps``.

Fee model: taker fees are charged in the quote currency of each leg.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import combinations

from arbies.arbitrage.depth import fill_by_base, fill_by_quote, max_crossing_amount
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


class CrossExchangeDetector:
    def __init__(self, params: StrategyParams, rules: dict[tuple[str, str], MarketRules] | None = None,
                 valuator: Valuator | None = None):
        self.params = params
        self.rules = rules or {}
        self.valuator = valuator or Valuator()

    # ------------------------------------------------------------------ helpers
    def _quote_group(self, quote: str) -> str:
        for group in self.params.quote_equivalents:
            if quote in group:
                return "|".join(sorted(group))
        return quote

    def _fee(self, exchange: str, symbol: str) -> Decimal:
        r = self.rules.get((exchange, symbol))
        return self.params.taker_fee(exchange, symbol, r.taker_fee if r else DEFAULT_TAKER)

    def _round(self, amount: Decimal, *rules: MarketRules | None) -> Decimal:
        for r in rules:
            if r is not None:
                amount = r.round_amount(amount)
        return amount

    # ------------------------------------------------------------------ public
    def evaluate(self, books: dict[tuple[str, str], OrderBook], now: datetime,
                 balances: BalanceFn | None = None, health: HealthFn | None = None) -> list[Opportunity]:
        groups: dict[tuple[str, str], list[OrderBook]] = {}
        for (_ex, sym), ob in books.items():
            base, quote = split_symbol(sym)
            groups.setdefault((base, self._quote_group(quote)), []).append(ob)
        out: list[Opportunity] = []
        for obs in groups.values():
            for a, b in combinations(obs, 2):
                if a.exchange == b.exchange:
                    continue
                if not (a.asks and a.bids and b.asks and b.bids):
                    continue
                # pick the direction with the larger top-of-book discrepancy
                s_ab = b.bids[0].price - a.asks[0].price
                s_ba = a.bids[0].price - b.asks[0].price
                buy, sell = (a, b) if s_ab >= s_ba else (b, a)
                out.append(self.evaluate_pair(buy, sell, now, balances, health))
        return out

    def evaluate_pair(self, buy_book: OrderBook, sell_book: OrderBook, now: datetime,
                      balances: BalanceFn | None = None, health: HealthFn | None = None) -> Opportunity:
        p = self.params
        base, quote_a = split_symbol(buy_book.symbol)
        _, quote_b = split_symbol(sell_book.symbol)
        ask_a = buy_book.asks[0].price
        bid_b = sell_book.bids[0].price
        fee_a = self._fee(buy_book.exchange, buy_book.symbol)
        fee_b = self._fee(sell_book.exchange, sell_book.symbol)
        rules_a = self.rules.get((buy_book.exchange, buy_book.symbol))
        rules_b = self.rules.get((sell_book.exchange, sell_book.symbol))

        age = max(buy_book.age_ms(now), sell_book.age_ms(now))
        confidence = confidence_from_age(age, p.max_data_age_ms)
        if buy_book.is_crossed() or sell_book.is_crossed():
            confidence = 0.0

        opp = Opportunity(
            kind=StrategyKind.CROSS_EXCHANGE,
            key=Opportunity.make_key(StrategyKind.CROSS_EXCHANGE,
                                     [buy_book.exchange, buy_book.symbol, sell_book.exchange, sell_book.symbol]),
            exchanges=[buy_book.exchange, sell_book.exchange],
            symbols=sorted({buy_book.symbol, sell_book.symbol}),
            detected_at=now,
            profit_currency=quote_a,
            buy_exchange=buy_book.exchange,
            sell_exchange=sell_book.exchange,
            route=[quote_a, base, quote_b],
            gross_spread_pct=(bid_b - ask_a) / ask_a * 100,
            data_age_ms=age,
            confidence=confidence,
            data_source=buy_book.source.value if buy_book.source == sell_book.source else "mixed",
        )

        if age > p.max_data_age_ms:
            opp.reject(RejectionReason.STALE_DATA)
        if health is not None and not (health(buy_book.exchange) and health(sell_book.exchange)):
            opp.reject(RejectionReason.EXCHANGE_UNAVAILABLE)

        if bid_b <= ask_a:
            opp.reject(RejectionReason.SPREAD_NON_POSITIVE)
            return opp

        max_quote = self.valuator.from_reporting(quote_a, p.max_trade_gbp)
        if max_quote is None:
            opp.reject(RejectionReason.NO_VALUATION)
            return opp
        cap_trade = max_quote / ask_a
        crossing = max_crossing_amount(buy_book.asks, sell_book.bids)
        market_cap = min(cap_trade, crossing)

        inv_cap = None
        if balances is not None:
            q_bal = balances(buy_book.exchange, quote_a)
            b_bal = balances(sell_book.exchange, base)
            caps = []
            if q_bal is not None:
                caps.append(fill_by_quote(buy_book.asks, q_bal / (1 + fee_a)).base_amount if q_bal > 0 else ZERO)
            if b_bal is not None:
                caps.append(max(b_bal, ZERO))
            if caps:
                inv_cap = min(caps)

        max_size = market_cap if inv_cap is None else min(market_cap, inv_cap)
        if inv_cap is not None and self._round(inv_cap, rules_a, rules_b) <= 0:
            opp.reject(RejectionReason.INSUFFICIENT_INVENTORY)
            max_size = market_cap  # still show what the market would have offered
        max_size = self._round(max_size, rules_a, rules_b)
        opp.max_size_base = max_size
        if max_size <= 0:
            opp.reject(RejectionReason.INSUFFICIENT_LIQUIDITY)
            return opp

        # candidate sizes at every level boundary up to max_size
        cands: set[Decimal] = {max_size}
        for levels in (buy_book.asks, sell_book.bids):
            total = ZERO
            for lv in levels:
                total += lv.amount
                if total >= max_size:
                    break
                cands.add(self._round(total, rules_a, rules_b))
        best = None
        for size in sorted(c for c in cands if c > 0):
            res = self._evaluate_size(size, buy_book, sell_book, fee_a, fee_b, quote_a, quote_b)
            if res is None:
                continue
            if best is None or res["net"] > best["net"]:
                best = res
        if best is None:
            opp.reject(RejectionReason.INSUFFICIENT_LIQUIDITY)
            return opp

        size = best["size"]
        opp.size_base = size
        opp.notional = best["buy_quote"]
        opp.gross_profit = best["gross"]
        opp.fees = best["fees"]
        opp.depth_slippage = best["depth_slip"]
        opp.slippage_buffer = best["slip"]
        opp.latency_cost = best["latency"]
        opp.rebalancing_cost = best["rebal"]
        opp.fx_cost = best["fx"]
        opp.leg_failure_cost = best["legfail"]
        opp.net_profit = best["net"]
        opp.net_return_pct = best["net"] / best["buy_quote"] * 100 if best["buy_quote"] > 0 else ZERO
        opp.legs = [
            Leg(buy_book.exchange, buy_book.symbol, Side.BUY, size, best["buy_vwap"], best["buy_quote"],
                best["buy_fee"], quote_a, ask_a, best["buy_levels"]),
            Leg(sell_book.exchange, sell_book.symbol, Side.SELL, size, best["sell_vwap"], best["sell_quote"],
                best["sell_fee"], quote_b, bid_b, best["sell_levels"]),
        ]
        ngbp, _ = self.valuator.value(quote_a, opp.net_profit)
        opp.net_profit_gbp = ngbp
        opp.notional_gbp, _ = self.valuator.value(quote_a, opp.notional)

        if (rules_a and not rules_a.meets_minimums(size, best["buy_vwap"])) or (
                rules_b and not rules_b.meets_minimums(size, best["sell_vwap"])):
            opp.reject(RejectionReason.BELOW_MIN_ORDER)
        if opp.gross_profit <= 0:
            opp.reject(RejectionReason.INSUFFICIENT_LIQUIDITY)
        elif opp.net_profit <= 0:
            opp.reject(RejectionReason.FEES_EXCEED_PROFIT)
        elif opp.net_return_pct < p.min_net_profit_pct:
            opp.reject(RejectionReason.BELOW_THRESHOLD)
        opp.qualified = not opp.rejection_reasons
        return opp

    def _evaluate_size(self, size, buy_book, sell_book, fee_a, fee_b, quote_a, quote_b):
        p = self.params
        buy = fill_by_base(buy_book.asks, size, is_buy=True)
        sell = fill_by_base(sell_book.bids, size, is_buy=False)
        if not (buy.fully_filled and sell.fully_filled):
            return None
        buy_fee = buy.quote_amount * fee_a
        sell_fee = sell.quote_amount * fee_b
        gross = sell.quote_amount - buy.quote_amount
        both = buy.quote_amount + sell.quote_amount
        slip = both * p.slippage_buffer_bps / BPS
        latency = both * p.latency_cost_bps / BPS
        rebal = buy.quote_amount * p.rebalancing_cost_bps / BPS
        fx = sell.quote_amount * p.fx_cost_bps / BPS if quote_a != quote_b else ZERO
        legfail = buy.quote_amount * p.leg_failure_prob * p.leg_failure_loss_bps / BPS
        fees = buy_fee + sell_fee
        net = gross - fees - slip - latency - rebal - fx - legfail
        depth_slip = (buy.quote_amount - size * buy_book.asks[0].price) + (size * sell_book.bids[0].price - sell.quote_amount)
        return {
            "size": size, "buy_quote": buy.quote_amount, "sell_quote": sell.quote_amount,
            "buy_vwap": buy.vwap, "sell_vwap": sell.vwap, "buy_fee": buy_fee, "sell_fee": sell_fee,
            "buy_levels": buy.levels_used, "sell_levels": sell.levels_used,
            "gross": gross, "fees": fees, "slip": slip, "latency": latency, "rebal": rebal, "fx": fx,
            "legfail": legfail, "net": net, "depth_slip": depth_slip,
        }
