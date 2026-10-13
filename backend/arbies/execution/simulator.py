"""Simulated order execution against order-book snapshots.

Used identically by the backtester and the paper trader so that both share the
same execution assumptions:

* Orders are submitted at decision time and fill against the book observed
  *at execution time* (decision time + simulated latency), never the book the
  decision was based on.
* Taker orders are IOC limit orders: they consume book levels up to the limit
  price, so they may partially fill.
* Maker orders rest at the limit price and fill with a configurable,
  deterministic-seeded probability (a crude queue model - documented as such).
* Cross-exchange legs execute simultaneously; if fills mismatch, the excess is
  hedged (unwound with an unlimited taker order) or held, per policy.
* Triangular legs execute sequentially, each one latency window apart. A failed
  leg leaves the remainder in an intermediate currency which is unwound back to
  the start currency (or held).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from arbies.arbitrage.depth import fill_by_base, fill_by_quote
from arbies.arbitrage.models import Opportunity, StrategyKind
from arbies.core.models import ZERO, MarketRules, OrderBook, Side, split_symbol
from arbies.core.valuation import Valuator
from arbies.execution.portfolio import InsufficientBalance, Portfolio

DEFAULT_TAKER = Decimal("0.0026")


class HedgePolicy(StrEnum):
    UNWIND = "unwind"  # immediately reverse the unmatched quantity with a taker order
    HOLD = "hold"  # keep the inventory (shows up as exposure / unrealised P&L)


class FillModel(BaseModel):
    order_type: str = "taker"  # taker | maker
    maker_fill_probability: float = 0.5
    max_slippage_pct: Decimal = Decimal("0.20")  # limit price tolerance vs estimated VWAP
    latency_ms: int = 250
    latency_jitter_ms: int = 100
    hedge_policy: HedgePolicy = HedgePolicy.UNWIND
    seed: int = 42


class OrderStatus(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    UNFILLED = "unfilled"
    REJECTED = "rejected"  # e.g. insufficient balance, no book


@dataclass(slots=True)
class SimFill:
    client_order_id: str
    ts: datetime
    exchange: str
    symbol: str
    side: Side
    requested_base: Decimal
    filled_base: Decimal
    quote_amount: Decimal
    fee: Decimal
    fee_currency: str
    limit_price: Decimal | None
    status: OrderStatus
    purpose: str  # entry | hedge | unwind
    order_type: str = "taker"
    reason: str = ""

    @property
    def vwap(self) -> Decimal | None:
        return self.quote_amount / self.filled_base if self.filled_base > 0 else None

    def to_dict(self) -> dict:
        return {
            "client_order_id": self.client_order_id, "ts": self.ts.isoformat(), "exchange": self.exchange,
            "symbol": self.symbol, "side": self.side.value, "requested_base": str(self.requested_base),
            "filled_base": str(self.filled_base), "quote_amount": str(self.quote_amount),
            "vwap": str(self.vwap) if self.vwap is not None else None, "fee": str(self.fee),
            "fee_currency": self.fee_currency, "limit_price": str(self.limit_price) if self.limit_price else None,
            "status": self.status.value, "purpose": self.purpose, "order_type": self.order_type, "reason": self.reason,
        }


class ExecutionSimulator:
    def __init__(self, model: FillModel, rules: dict[tuple[str, str], MarketRules] | None = None,
                 fee_lookup: Callable[[str, str], Decimal] | None = None):
        self.model = model
        self.rules = rules or {}
        self.rng = random.Random(model.seed)
        self._fee_lookup = fee_lookup
        self._order_seq = 0

    def sample_latency(self) -> timedelta:
        jitter = self.rng.uniform(0, self.model.latency_jitter_ms) if self.model.latency_jitter_ms else 0
        return timedelta(milliseconds=self.model.latency_ms + jitter)

    def next_client_id(self, prefix: str) -> str:
        self._order_seq += 1
        return f"{prefix}-{self._order_seq:08d}"

    # ------------------------------------------------------------------ single order
    def execute_order(self, portfolio: Portfolio, book: OrderBook | None, side: Side, now: datetime, *,
                      base_amount: Decimal | None = None, quote_amount: Decimal | None = None,
                      limit_price: Decimal | None = None, fee_in_quote: bool = True, purpose: str = "entry",
                      order_type: str | None = None, ref: str = "", exchange: str = "", symbol: str = ""
                      ) -> SimFill:
        """Execute an order against ``book`` and apply it to ``portfolio``.

        Exactly one of ``base_amount`` / ``quote_amount`` (buy only) is given.
        """
        order_type = order_type or self.model.order_type
        exchange = book.exchange if book else exchange
        symbol = book.symbol if book else symbol
        cid = self.next_client_id(purpose)
        base, quote = split_symbol(symbol)
        req = base_amount if base_amount is not None else ZERO
        fee_ccy = quote if fee_in_quote else (base if side == Side.BUY else quote)

        def result(status, filled=ZERO, q=ZERO, fee=ZERO, reason=""):
            return SimFill(cid, now, exchange, symbol, side, req, filled, q, fee, fee_ccy, limit_price, status,
                           purpose, order_type, reason)

        if book is None or (side == Side.BUY and not book.asks) or (side == Side.SELL and not book.bids):
            return result(OrderStatus.REJECTED, reason="no order book")
        rules = self.rules.get((exchange, symbol))
        if order_type == "maker":
            fee_rate = rules.maker_fee if rules else DEFAULT_TAKER
        elif self._fee_lookup is not None:
            fee_rate = self._fee_lookup(exchange, symbol)
        else:
            fee_rate = rules.taker_fee if rules else DEFAULT_TAKER

        if order_type == "maker":
            # Resting limit order: fills completely at the limit price with probability p, else not at all.
            px = limit_price or (book.best_bid if side == Side.BUY else book.best_ask)
            if base_amount is None and quote_amount is not None:
                base_amount = quote_amount / px
                req = base_amount
            if rules:
                base_amount = rules.round_amount(base_amount)
            if self.rng.random() > self.model.maker_fill_probability:
                return result(OrderStatus.UNFILLED, reason="maker order not filled in window")
            filled, q = base_amount, base_amount * px
        else:
            levels = book.asks if side == Side.BUY else book.bids
            if base_amount is None:
                assert quote_amount is not None and side == Side.BUY
                unlimited = fill_by_quote(levels, quote_amount).base_amount
                req = rules.round_amount(unlimited) if rules else unlimited
                est = fill_by_quote(levels, quote_amount, limit_price)
                filled = rules.round_amount(est.base_amount) if rules else est.base_amount
                est = fill_by_base(levels, filled, limit_price, is_buy=True)
            else:
                amt = rules.round_amount(base_amount) if rules else base_amount
                est = fill_by_base(levels, amt, limit_price, is_buy=(side == Side.BUY))
            filled, q = est.base_amount, est.quote_amount
            if rules and filled > 0 and not rules.meets_minimums(filled, q / filled):
                return result(OrderStatus.REJECTED, reason="below exchange minimum")
        if filled <= 0:
            return result(OrderStatus.UNFILLED, reason="no liquidity within limit price")

        fee = (q * fee_rate) if fee_ccy == quote else (filled * fee_rate if side == Side.BUY else q * fee_rate)
        try:
            portfolio.apply_trade(now, exchange, symbol, side, filled, q, fee, fee_ccy, ref or cid)
        except InsufficientBalance as e:
            return result(OrderStatus.REJECTED, reason=str(e))
        status = OrderStatus.FILLED if req == 0 or filled >= req else OrderStatus.PARTIAL
        return result(status, filled, q, fee)


class JobStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"  # all legs fully filled
    PARTIAL_HEDGED = "partial_hedged"  # mismatch occurred and was unwound
    PARTIAL_HELD = "partial_held"  # mismatch held as inventory
    FAILED = "failed"  # nothing (useful) executed


BookGetter = Callable[[str, str], OrderBook | None]


@dataclass
class ExecutionJob:
    """State machine executing one opportunity over simulated time."""

    opp: Opportunity
    created: datetime
    latency: timedelta
    job_id: str
    next_due: datetime
    stage: int = 0
    fills: list[SimFill] = field(default_factory=list)
    status: JobStatus = JobStatus.PENDING
    value_before_gbp: Decimal = ZERO
    value_after_gbp: Decimal = ZERO
    pnl_gbp: Decimal = ZERO  # portfolio value change at constant execution marks
    fees_gbp: Decimal = ZERO
    hedge_loss_gbp: Decimal = ZERO
    slippage_gbp: Decimal = ZERO  # realised vs estimated cost difference
    notes: list[str] = field(default_factory=list)
    # internal
    _carry: Decimal = ZERO  # amount carried between triangular legs
    _marks: Valuator | None = None
    _mismatch: Decimal = ZERO

    @property
    def done(self) -> bool:
        return self.status != JobStatus.PENDING

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id, "opportunity_key": self.opp.key, "kind": self.opp.kind.value,
            "route": self.opp.route, "exchanges": self.opp.exchanges, "symbols": self.opp.symbols,
            "created": self.created.isoformat(), "status": self.status.value,
            "expected_net_profit_gbp": str(self.opp.net_profit_gbp) if self.opp.net_profit_gbp is not None else None,
            "expected_net_return_pct": str(self.opp.net_return_pct),
            "notional_gbp": str(self.opp.notional_gbp) if self.opp.notional_gbp is not None else None,
            "pnl_gbp": str(self.pnl_gbp), "fees_gbp": str(self.fees_gbp),
            "hedge_loss_gbp": str(self.hedge_loss_gbp), "slippage_gbp": str(self.slippage_gbp),
            "latency_ms": self.latency.total_seconds() * 1000, "fills": [f.to_dict() for f in self.fills],
            "notes": self.notes,
        }


class ArbitrageExecutor:
    """Drives ExecutionJobs through their legs."""

    def __init__(self, sim: ExecutionSimulator, reporting: str = "GBP"):
        self.sim = sim
        self.reporting = reporting
        self._job_seq = 0

    def start(self, opp: Opportunity, portfolio: Portfolio, valuator: Valuator, now: datetime) -> ExecutionJob:
        self._job_seq += 1
        lat = self.sim.sample_latency()
        job = ExecutionJob(opp=opp, created=now, latency=lat, job_id=f"job-{self._job_seq:08d}",
                           next_due=now + lat)
        # freeze valuation marks at decision time so P&L reflects execution quality,
        # not unrelated market moves of the rest of the inventory
        marks = Valuator(valuator.reporting, valuator.fallback)
        marks._edges = {k: dict(v) for k, v in valuator._edges.items()}
        job._marks = marks
        job.value_before_gbp, _ = portfolio.total_value(marks)
        return job

    def step(self, job: ExecutionJob, portfolio: Portfolio, get_book: BookGetter, now: datetime) -> None:
        if job.done or now < job.next_due:
            return
        if job.opp.kind == StrategyKind.CROSS_EXCHANGE:
            self._step_cross(job, portfolio, get_book, now)
        else:
            self._step_triangular(job, portfolio, get_book, now)
        if job.done:
            self._finalise(job, portfolio)

    # ------------------------------------------------------------------ cross
    def _step_cross(self, job: ExecutionJob, pf: Portfolio, get_book: BookGetter, now: datetime) -> None:
        opp = job.opp
        tol = self.sim.model.max_slippage_pct / 100
        buy_leg, sell_leg = opp.legs
        if job.stage == 0:
            bb = get_book(buy_leg.exchange, buy_leg.symbol)
            sb = get_book(sell_leg.exchange, sell_leg.symbol)
            f_buy = self.sim.execute_order(pf, bb, Side.BUY, now, base_amount=buy_leg.base_amount,
                                           limit_price=buy_leg.vwap * (1 + tol), purpose="entry", ref=job.job_id,
                                           exchange=buy_leg.exchange, symbol=buy_leg.symbol)
            f_sell = self.sim.execute_order(pf, sb, Side.SELL, now, base_amount=sell_leg.base_amount,
                                            limit_price=sell_leg.vwap * (1 - tol), purpose="entry", ref=job.job_id,
                                            exchange=sell_leg.exchange, symbol=sell_leg.symbol)
            job.fills += [f_buy, f_sell]
            job._mismatch = f_buy.filled_base - f_sell.filled_base
            if f_buy.filled_base == 0 and f_sell.filled_base == 0:
                job.status = JobStatus.FAILED
                job.notes.append("neither leg filled")
                return
            if job._mismatch == 0:
                job.status = JobStatus.SUCCESS
                return
            job.notes.append(f"leg mismatch of {job._mismatch} base units")
            if self.sim.model.hedge_policy == HedgePolicy.HOLD:
                job.status = JobStatus.PARTIAL_HELD
                return
            job.stage = 1
            job.next_due = now + self.sim.sample_latency()
            return
        # stage 1: hedge the mismatch
        if job._mismatch > 0:  # bought more than sold: sell excess where it was bought
            book = get_book(buy_leg.exchange, buy_leg.symbol)
            f = self.sim.execute_order(pf, book, Side.SELL, now, base_amount=job._mismatch, purpose="hedge",
                                       order_type="taker", ref=job.job_id, exchange=buy_leg.exchange,
                                       symbol=buy_leg.symbol)
        else:  # sold more than bought: buy back on the sell exchange
            book = get_book(sell_leg.exchange, sell_leg.symbol)
            f = self.sim.execute_order(pf, book, Side.BUY, now, base_amount=-job._mismatch, purpose="hedge",
                                       order_type="taker", ref=job.job_id, exchange=sell_leg.exchange,
                                       symbol=sell_leg.symbol)
        job.fills.append(f)
        if f.status in (OrderStatus.FILLED,):
            job.status = JobStatus.PARTIAL_HEDGED
        else:
            job.status = JobStatus.PARTIAL_HELD
            job.notes.append(f"hedge incomplete: {f.status.value} {f.reason}")

    # ------------------------------------------------------------------ triangular
    def _step_triangular(self, job: ExecutionJob, pf: Portfolio, get_book: BookGetter, now: datetime) -> None:
        opp = job.opp
        tol = self.sim.model.max_slippage_pct / 100
        legs = opp.legs
        if job.stage < len(legs):
            leg = legs[job.stage]
            book = get_book(leg.exchange, leg.symbol)
            if job.stage == 0:
                amount = leg.quote_amount if leg.side == Side.BUY else leg.base_amount
            else:
                amount = job._carry
            if leg.side == Side.BUY:
                f = self.sim.execute_order(pf, book, Side.BUY, now, quote_amount=amount,
                                           limit_price=leg.vwap * (1 + tol), fee_in_quote=False,
                                           purpose="entry", ref=job.job_id, exchange=leg.exchange, symbol=leg.symbol)
                received = f.filled_base - f.fee
            else:
                f = self.sim.execute_order(pf, book, Side.SELL, now, base_amount=amount,
                                           limit_price=leg.vwap * (1 - tol), fee_in_quote=False,
                                           purpose="entry", ref=job.job_id, exchange=leg.exchange, symbol=leg.symbol)
                received = f.quote_amount - f.fee
            job.fills.append(f)
            if f.filled_base <= 0:
                if job.stage == 0:
                    job.status = JobStatus.FAILED
                    job.notes.append(f"first leg not filled: {f.reason}")
                    return
                job.notes.append(f"leg {job.stage + 1} not filled: {f.reason}")
                job.stage = 10  # go to unwind
                job.next_due = now + self.sim.sample_latency()
                return
            if f.status == OrderStatus.PARTIAL:
                job.notes.append(f"leg {job.stage + 1} partially filled")
            job._carry = received
            job.stage += 1
            if job.stage == len(legs):
                partial = any(x.status == OrderStatus.PARTIAL for x in job.fills)
                if partial:
                    job.stage = 10
                    job.next_due = now + self.sim.sample_latency()
                else:
                    job.status = JobStatus.SUCCESS
                return
            job.next_due = now + self.sim.sample_latency()
            return
        # unwind stage: convert any residual intermediate currencies back to the start currency
        start = opp.route[0]
        if self.sim.model.hedge_policy == HedgePolicy.HOLD:
            job.status = JobStatus.PARTIAL_HELD
            return
        exchange = opp.exchanges[0]
        unwound_all = True
        for ccy in opp.route[1:-1]:
            # residual generated by this job: net flow of the currency across its fills
            flow = ZERO
            for f in job.fills:
                b, q = split_symbol(f.symbol)
                if b == ccy:
                    flow += f.filled_base if f.side == Side.BUY else -f.filled_base
                if q == ccy:
                    flow += -f.quote_amount if f.side == Side.BUY else f.quote_amount
                if f.fee_currency == ccy:
                    flow -= f.fee
            flow = min(flow, pf.available(exchange, ccy))
            if flow <= 0:
                continue
            sym_sell, sym_buy = f"{ccy}/{start}", f"{start}/{ccy}"
            b1 = get_book(exchange, sym_sell)
            if b1 is not None:
                f = self.sim.execute_order(pf, b1, Side.SELL, now, base_amount=flow, purpose="unwind",
                                           order_type="taker", fee_in_quote=False, ref=job.job_id)
            else:
                b2 = get_book(exchange, sym_buy)
                if b2 is None:
                    unwound_all = False
                    job.notes.append(f"no direct market to unwind {ccy} -> {start}")
                    continue
                f = self.sim.execute_order(pf, b2, Side.BUY, now, quote_amount=flow, purpose="unwind",
                                           order_type="taker", fee_in_quote=False, ref=job.job_id)
            job.fills.append(f)
            if f.status != OrderStatus.FILLED:
                unwound_all = False
        job.status = JobStatus.PARTIAL_HEDGED if unwound_all else JobStatus.PARTIAL_HELD

    # ------------------------------------------------------------------ accounting
    def _finalise(self, job: ExecutionJob, pf: Portfolio) -> None:
        marks = job._marks
        assert marks is not None
        job.value_after_gbp, _ = pf.total_value(marks)
        job.pnl_gbp = job.value_after_gbp - job.value_before_gbp
        fees = ZERO
        hedge_pnl = ZERO
        for f in job.fills:
            fees += marks.to_reporting(f.fee_currency, f.fee)
        for f in job.fills:
            if f.purpose in ("hedge", "unwind") and f.filled_base > 0:
                hedge_pnl += self._fill_value_change(f, marks)
        job.fees_gbp = fees
        job.hedge_loss_gbp = -hedge_pnl if hedge_pnl < 0 else ZERO
        if job.opp.net_profit_gbp is not None and job.status == JobStatus.SUCCESS:
            # positive = realised worse than estimated
            est_gross_less_fees = job.opp.net_profit_gbp + _gbp(job.opp, job.opp.slippage_buffer + job.opp.latency_cost
                                                               + job.opp.rebalancing_cost + job.opp.fx_cost
                                                               + job.opp.leg_failure_cost, marks)
            job.slippage_gbp = est_gross_less_fees - job.pnl_gbp

    @staticmethod
    def _fill_value_change(f: SimFill, marks: Valuator) -> Decimal:
        base, quote = split_symbol(f.symbol)
        b = marks.to_reporting(base, f.filled_base)
        q = marks.to_reporting(quote, f.quote_amount)
        fee = marks.to_reporting(f.fee_currency, f.fee)
        return (b - q - fee) if f.side == Side.BUY else (q - b - fee)


def _gbp(opp: Opportunity, amount: Decimal, marks: Valuator) -> Decimal:
    return marks.to_reporting(opp.profit_currency, amount)
