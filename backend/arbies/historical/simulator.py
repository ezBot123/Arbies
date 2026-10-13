"""Long-horizon historical investment simulation with recurring personal contributions.

One *variant* = one strategy set (A cross-exchange, B triangular, C combined) with one parameter preset and
one fee schedule. Every variant replays the same genuine order-book events through the same deterministic
``TradingCore`` used by backtests and paper trading, against a single portfolio that receives the same
contribution schedule.

Integrity rules
* Order-book events are replayed in ``received_ts`` order; decisions only see books received at or before
  "now" (``TradingCore`` raises ``LookaheadError`` otherwise).
* Trading only happens where genuine order-book data exists (``live_recorded`` / ``third_party_imported``).
  Where there is none, the bot is idle: no trade, no opportunity and no P&L is ever invented.
* Inventory is valued with fresh order-book mids when available, otherwise with the close of the most recent
  *completed* daily exchange candle (reference price). Reference prices never drive trading decisions.
* Deposits are capital flows. They are converted into the target allocation at the prevailing reference price
  minus an explicit conversion cost and are reported separately from trading P&L and market P&L.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from arbies.arbitrage.models import StrategyParams
from arbies.backtest.config import AllocationItem, RebalanceConfig
from arbies.backtest.metrics import compute_metrics
from arbies.core.models import DataSource, MarketRules, OrderBook, split_symbol
from arbies.execution.portfolio import Portfolio
from arbies.execution.simulator import ExecutionJob, FillModel, JobStatus
from arbies.historical.plan import (
    ContributionPlan,
    FeeSchedule,
    ValuePoint,
    default_fee_schedules,
    time_weighted_return,
    xirr,
)
from arbies.historical.reference import DailyMarks
from arbies.risk.engine import RiskLimits
from arbies.services.trading_core import TradingCore

ZERO = Decimal(0)
Q8 = Decimal("0.00000001")
FIAT = ("GBP", "USD", "EUR", "USDC", "USDT")
VALUATION_SYMBOLS = ["BTC/GBP", "ETH/GBP", "GBP/USD", "BTC/USD", "ETH/USD", "ETH/BTC"]
BOOK_VALUATION_MAX_AGE = timedelta(seconds=60)

STRATEGY_SETS = {
    "cross_exchange": ["cross_exchange"],
    "triangular": ["triangular"],
    "combined": ["cross_exchange", "triangular"],
}
STRATEGY_LABELS = {"cross_exchange": "A: cross-exchange", "triangular": "B: triangular", "combined": "C: combined"}

PRESETS: dict[str, dict] = {
    "conservative": {"params": {"min_net_profit_pct": "0.50", "max_trade_gbp": "15", "latency_ms": 500,
                                "slippage_buffer_bps": "10", "leg_failure_prob": "0.05"},
                     "risk": {"min_net_return_pct": "0.50", "max_position_gbp": "15"},
                     "fill": {"latency_ms": 500, "latency_jitter_ms": 200}},
    "moderate": {},
    "aggressive": {"params": {"min_net_profit_pct": "0.10", "max_trade_gbp": "50", "latency_ms": 100,
                              "slippage_buffer_bps": "3"},
                   "risk": {"min_net_return_pct": "0.10", "max_position_gbp": "50", "min_confidence": 0.1},
                   "fill": {"latency_ms": 100, "latency_jitter_ms": 50}},
}


def q(x: Decimal) -> str:
    return str(x.quantize(Q8))


class SimulationConfig(BaseModel):
    name: str = "£500 + £100/month, Jan 2025 - Oct 2026"
    start: datetime = datetime(2025, 1, 1, tzinfo=UTC)
    end: datetime = datetime(2026, 10, 13, 23, 59, 59, tzinfo=UTC)
    plan: ContributionPlan = Field(default_factory=ContributionPlan)
    exchanges: list[str] = Field(default_factory=lambda: ["kraken", "coinbase", "bitstamp"])
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USD", "ETH/USD", "ETH/BTC", "BTC/GBP", "ETH/GBP"])
    data_sources: list[DataSource] = Field(default_factory=lambda: [DataSource.LIVE, DataSource.IMPORTED])
    strategies: list[str] = Field(default_factory=lambda: ["cross_exchange", "triangular", "combined"])
    preset: str = "moderate"
    fee_schedule: str = "base"  # base | low | high | custom
    custom_fees: FeeSchedule | None = None
    run_parameter_study: bool = True
    params: StrategyParams = Field(default_factory=StrategyParams)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    fill: FillModel = Field(default_factory=FillModel)
    rebalance: RebalanceConfig = Field(default_factory=RebalanceConfig)
    crypto_inventory_pct: Decimal = Decimal("50")  # share of each deposit held as crypto inventory
    scale_risk_with_capital: bool = True
    reference_exchange: str = "kraken"
    reference_fallbacks: list[str] = Field(default_factory=lambda: ["bitstamp", "coinbase"])
    btc_benchmark_exchange: str = "kraken"
    btc_benchmark_spread_bps: Decimal = Decimal("10")  # half-spread/slippage allowance on benchmark buys
    decision_interval_ms: int = 500
    train_fraction: float = Field(0.6, gt=0.1, lt=0.9)
    seed: int = 42

    @model_validator(mode="after")
    def _check(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if DataSource.SYNTHETIC in self.data_sources:
            raise ValueError("historical investment simulations never use synthetic data")
        for s in self.strategies:
            if s not in STRATEGY_SETS:
                raise ValueError(f"unknown strategy set {s}")
        if self.preset not in PRESETS:
            raise ValueError(f"unknown preset {self.preset}")
        if self.fee_schedule == "custom" and self.custom_fees is None:
            raise ValueError("custom fee schedule selected but none supplied")
        if not (Decimal(0) <= self.crypto_inventory_pct <= Decimal(100)):
            raise ValueError("crypto_inventory_pct must be within 0..100")
        self.fill.seed = self.seed
        return self

    def fees(self, name: str | None = None) -> FeeSchedule:
        name = name or self.fee_schedule
        if name == "custom":
            return self.custom_fees
        return default_fee_schedules()[name]

    def allocation(self) -> list[AllocationItem]:
        quotes, bases = [], []
        for s in self.symbols:
            b, qq = split_symbol(s)
            if qq in FIAT and qq not in quotes:
                quotes.append(qq)
            if b not in FIAT and b not in bases:
                bases.append(b)
        crypto = self.crypto_inventory_pct / 100 if bases else ZERO
        share = Decimal(1) / len(self.exchanges)
        out = []
        for ex in self.exchanges:
            for f in quotes or ["GBP"]:
                out.append(AllocationItem(exchange=ex, asset=f, fraction=share * (1 - crypto) / len(quotes or ["GBP"])))
            for c in bases:
                out.append(AllocationItem(exchange=ex, asset=c, fraction=share * crypto / len(bases)))
        return out


def preset_models(cfg: SimulationConfig, preset: str) -> tuple[StrategyParams, RiskLimits, FillModel]:
    p = PRESETS[preset]
    params = cfg.params.model_copy(update={k: (Decimal(v) if isinstance(v, str) else v)
                                            for k, v in p.get("params", {}).items()}, deep=True)
    params.fee_overrides = dict(params.fee_overrides)
    risk = cfg.risk.model_copy(update={k: (Decimal(v) if isinstance(v, str) else v) for k, v in p.get("risk", {}).items()})
    fill = cfg.fill.model_copy(update=p.get("fill", {}))
    return params, risk, fill


# ============================================================================ variant replay
@dataclass
class MonthStats:
    month: str
    start_value: Decimal = ZERO
    end_value: Decimal = ZERO
    deposits: Decimal = ZERO
    conversion_costs: Decimal = ZERO
    trading_pnl: Decimal = ZERO
    fees: Decimal = ZERO
    slippage: Decimal = ZERO
    hedge_losses: Decimal = ZERO
    rebalancing_costs: Decimal = ZERO
    trades: int = 0
    successful: int = 0
    failed: int = 0
    partial: int = 0
    observed_any_s: float = 0.0
    observed_two_s: float = 0.0
    seconds: float = 0.0
    book_events: int = 0
    closed: bool = False


@dataclass
class VariantResult:
    name: str
    summary: dict
    monthly: list[dict]
    equity: list[dict]
    breakdown: dict
    deposits: list[dict]
    jobs: list[ExecutionJob]
    warnings: list[str] = field(default_factory=list)
    result_hash: str = ""


def month_key(t: datetime) -> str:
    return f"{t.year}-{t.month:02d}"


class VariantReplay:
    """Sync replay driver. Feed book events in order with ``on_book``; call ``finish`` at the end."""

    def __init__(self, name: str, strategies: list[str], cfg: SimulationConfig, params: StrategyParams,
                 risk: RiskLimits, fill: FillModel, fees: FeeSchedule, rules: dict[tuple[str, str], MarketRules],
                 marks: DailyMarks):
        self.name = name
        self.cfg = cfg
        self.fees = fees
        self.marks = marks
        self.rules = copy.deepcopy(rules)
        self.params = params
        self.risk_limits = risk
        self.portfolio = Portfolio()
        self.core = TradingCore(strategies=strategies, params=params, rules=self.rules, fill=fill, risk_limits=risk,
                                portfolio=self.portfolio, mode="backtest", rebalance=cfg.rebalance,
                                max_concurrent_jobs_per_exchange=1)
        self.books: dict[tuple[str, str], OrderBook] = {}
        self.exchanges = set(cfg.exchanges)
        self.symbols = set(cfg.symbols)
        self.allocation = cfg.allocation()
        self.max_age = timedelta(milliseconds=params.max_data_age_ms)
        self.decision_iv = timedelta(milliseconds=cfg.decision_interval_ms)
        self.last_decision: datetime | None = None
        self.last_event: datetime | None = None
        self.warnings: list[str] = []
        self.months: dict[str, MonthStats] = {}
        self.points: list[ValuePoint] = []
        self.equity: list[dict] = []
        self.deposit_log: list[dict] = []
        self.cum_deposits = ZERO
        self.conversion_costs = ZERO
        self._jobs_seen = 0
        self._rebal_seen = ZERO
        self.events: list[tuple[datetime, int, str, object]] = self._clock_events()
        self._ev_i = 0
        self.now = cfg.start
        self._apply_fees(cfg.start)

    # ------------------------------------------------------------------ clock
    def _clock_events(self) -> list[tuple[datetime, int, str, object]]:
        cfg = self.cfg
        ev: list[tuple[datetime, int, str, object]] = []
        d = (cfg.start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        while d <= cfg.end:
            ev.append((d, 0, "tick", None))  # value the portfolio at the end of the previous day
            d += timedelta(days=1)
        ev.append((cfg.end, 0, "tick", None))
        for fd in self.fees.change_dates(cfg.start.date(), cfg.end.date()):
            ev.append((datetime(fd.year, fd.month, fd.day, tzinfo=UTC), 1, "fees", None))
        for ts, amount, label in cfg.plan.schedule(cfg.start, cfg.end):
            ev.append((ts, 2, "deposit", (amount, label)))
        ev.sort(key=lambda e: (e[0], e[1]))
        return ev

    def _month(self, t: datetime) -> MonthStats:
        k = month_key(t)
        m = self.months.get(k)
        if m is None:
            prev = [x for x in self.months.values() if x.month < k]
            m = MonthStats(k, start_value=(max(prev, key=lambda x: x.month).end_value if prev else ZERO))
            self.months[k] = m
        return m

    def advance(self, until: datetime, inclusive: bool) -> None:
        while self._ev_i < len(self.events):
            ts, _p, kind, payload = self.events[self._ev_i]
            if ts > until or (ts == until and not inclusive):
                break
            self._ev_i += 1
            self._settle_jobs(ts)
            self.now = ts
            if kind == "tick":
                self._tick(ts)
            elif kind == "fees":
                self._apply_fees(ts)
            elif kind == "deposit":
                self._deposit(ts, *payload)

    def _apply_fees(self, t: datetime) -> None:
        for ex in self.exchanges:
            e = self.fees.at(ex, t)
            if e is None:
                self.warnings.append(f"no fee schedule entry for {ex} at {t.date()}; exchange rules fee used")
                continue
            self.params.fee_overrides[ex] = e.taker
            for (rex, _sym), r in self.rules.items():
                if rex == ex:
                    r.taker_fee, r.maker_fee, r.fee_source = e.taker, e.maker, f"schedule:{self.fees.name}"

    # ------------------------------------------------------------------ valuation
    def _fresh_books(self, t: datetime) -> dict:
        return {k: ob for k, ob in self.books.items() if t - ob.received_ts <= BOOK_VALUATION_MAX_AGE}

    def _mark(self, t: datetime) -> tuple[Decimal, bool, str]:
        val = self.core.valuator
        val.base_prices = self.marks.edges(t)
        fresh = self._fresh_books(t)
        val.update_from_books(fresh.values())
        v, est = self.portfolio.total_value(val)
        return v, est, ("order_book+reference" if fresh else "reference")

    def _scale_risk(self, value: Decimal) -> None:
        if not self.cfg.scale_risk_with_capital or value <= 0:
            return
        r = self.risk_limits
        r.max_total_exposure_gbp = value
        r.max_exchange_exposure_gbp = value * Decimal("0.6")
        r.max_asset_exposure_gbp = value * Decimal("0.6")
        r.max_daily_loss_gbp = max(Decimal("5"), value * Decimal("0.05"))

    def _tick(self, t: datetime) -> None:
        v, est, src = self._mark(t)
        self._scale_risk(v)
        prev_day = t - timedelta(seconds=1)
        m = self._month(prev_day)
        m.end_value = v
        if t.day == 1 and t.hour == 0 and t.minute == 0 and t != self.cfg.start or t == self.cfg.end:
            m.closed = True
        self.points.append(ValuePoint(t, v, ZERO))
        self.equity.append({"ts": t.isoformat(), "value_gbp": q(v), "contributions_gbp": q(self.cum_deposits),
                            "net_gain_gbp": q(v - self.cum_deposits), "valuation": src, "estimated": est})

    def _deposit(self, t: datetime, amount: Decimal, label: str) -> None:
        val = self.core.valuator
        val.base_prices = self.marks.edges(t)
        fresh = self._fresh_books(t)
        val.update_from_books(fresh.values())
        cost_rate = self.cfg.plan.conversion_cost_bps / Decimal(10000)
        m = self._month(t)
        legs = []
        cost_total = ZERO
        for item in self.allocation:
            gbp = amount * item.fraction
            if gbp <= 0:
                continue
            if item.asset == self.cfg.plan.currency:
                self.portfolio.deposit(t, item.exchange, item.asset, gbp.quantize(Q8), ref=f"contribution:{label}")
                legs.append({"exchange": item.exchange, "asset": item.asset, "gbp": q(gbp), "amount": q(gbp)})
                continue
            r = val.rate(item.asset)
            if r is None or r.value <= 0:
                # cannot price the asset at this time: keep the money as cash rather than invent a price
                self.portfolio.deposit(t, item.exchange, self.cfg.plan.currency, gbp.quantize(Q8),
                                       ref=f"contribution:{label}:unpriced")
                self.warnings.append(f"{t.date()}: no {item.asset} price; deposit kept as {self.cfg.plan.currency}")
                continue
            cost = gbp * cost_rate
            amt = ((gbp - cost) / r.value).quantize(Q8)
            self.portfolio.deposit(t, item.exchange, item.asset, amt, ref=f"contribution:{label}")
            cost_total += cost
            legs.append({"exchange": item.exchange, "asset": item.asset, "gbp": q(gbp), "amount": str(amt),
                         "price_gbp": q(r.value), "conversion_cost_gbp": q(cost)})
        self.cum_deposits += amount
        self.conversion_costs += cost_total
        m.deposits += amount
        m.conversion_costs += cost_total
        v, _est, src = self._mark(t)
        self._scale_risk(v)
        self.points.append(ValuePoint(t, v, amount))
        self.deposit_log.append({"ts": t.isoformat(), "amount_gbp": q(amount), "label": label,
                                 "conversion_cost_gbp": q(cost_total), "priced_with": src, "legs": legs})

    # ------------------------------------------------------------------ trading
    def _health(self, now: datetime) -> dict[str, bool]:
        out = {}
        for ex in self.exchanges:
            newest = max((ob.received_ts for (e, _s), ob in self.books.items() if e == ex), default=None)
            out[ex] = newest is not None and now - newest <= self.max_age
        return out

    def _settle_jobs(self, until: datetime) -> None:
        while True:
            due = self.core.next_due()
            if due is None or due > until:
                break
            self.core.process_jobs(lambda e, s: self.books.get((e, s)), due)
        self._collect_jobs(until)

    def _collect_jobs(self, now: datetime) -> None:
        done = self.core.completed_jobs
        for j in done[self._jobs_seen:]:
            m = self._month(j.created)
            m.trades += 1
            m.trading_pnl += j.pnl_gbp
            m.fees += j.fees_gbp
            m.hedge_losses += j.hedge_loss_gbp
            if j.slippage_gbp > 0:
                m.slippage += j.slippage_gbp
            if j.status == JobStatus.SUCCESS:
                m.successful += 1
            elif j.status == JobStatus.FAILED:
                m.failed += 1
            else:
                m.partial += 1
        self._jobs_seen = len(done)
        d = self.core.rebalance_costs_gbp - self._rebal_seen
        if d:
            self._month(now).rebalancing_costs += d
            self._rebal_seen = self.core.rebalance_costs_gbp

    def on_book(self, ob: OrderBook) -> None:
        if ob.exchange not in self.exchanges or ob.symbol not in self.symbols:
            return
        now = ob.received_ts
        if self.last_event is not None and now < self.last_event:
            raise ValueError("order-book events are not in chronological order")
        self.advance(now, inclusive=True)
        self._settle_jobs(now)
        # observed-time accounting: time during which the bot had fresh data to act on
        health = self._health(now)
        if self.last_event is not None:
            dt = min((now - self.last_event).total_seconds(), self.max_age.total_seconds())
            m = self._month(now)
            if sum(health.values()) >= 1:
                m.observed_any_s += dt
            if sum(health.values()) >= 2:
                m.observed_two_s += dt
        self._month(now).book_events += 1
        self.books[(ob.exchange, ob.symbol)] = ob
        self.portfolio.settle_transfers(now)
        if self.last_decision is None or now - self.last_decision >= self.decision_iv:
            self.last_decision = now
            self.core.valuator.base_prices = self.marks.edges(now)
            self.core.decide(self.books, now, self._health(now))
            self.core.maybe_rebalance(now, self.cfg.exchanges)
        self.last_event = now
        self.now = now
        self._collect_jobs(now)

    def finish(self) -> VariantResult:
        self.advance(self.cfg.end, inclusive=True)
        guard = 0
        while self.core.active_jobs and guard < 100:
            due = self.core.next_due()
            self.core.process_jobs(lambda e, s: self.books.get((e, s)), due)
            guard += 1
        self._collect_jobs(self.cfg.end)
        return self._result()

    # ------------------------------------------------------------------ results
    def _result(self) -> VariantResult:
        cfg = self.cfg
        final_value, est, src = self._mark(cfg.end)
        jobs = self.core.completed_jobs
        months = [self.months[k] for k in sorted(self.months)]
        total_seconds = (cfg.end - cfg.start).total_seconds()
        for m in months:
            y, mo = map(int, m.month.split("-"))
            ms = datetime(y, mo, 1, tzinfo=UTC)
            me = datetime(y + (mo == 12), 1 if mo == 12 else mo + 1, 1, tzinfo=UTC)
            m.seconds = (min(me, cfg.end) - max(ms, cfg.start)).total_seconds()
        observed_any = sum(m.observed_any_s for m in months)
        observed_two = sum(m.observed_two_s for m in months)
        monthly = []
        for m in months:
            market = m.end_value - m.start_value - m.deposits + m.conversion_costs - m.trading_pnl + m.rebalancing_costs
            observed = m.observed_any_s > 0
            monthly.append({
                "month": m.month, "deposits_gbp": q(m.deposits),
                "trading_pnl_gbp": q(m.trading_pnl) if observed else None,
                "fees_gbp": q(m.fees) if observed else None,
                "slippage_gbp": q(m.slippage) if observed else None,
                "hedge_losses_gbp": q(m.hedge_losses) if observed else None,
                "rebalancing_costs_gbp": q(m.rebalancing_costs),
                "conversion_costs_gbp": q(m.conversion_costs),
                "market_pnl_gbp": q(market),
                "start_value_gbp": q(m.start_value), "ending_value_gbp": q(m.end_value),
                "trades": m.trades, "successful": m.successful, "failed": m.failed, "partial": m.partial,
                "observed_hours": round(m.observed_any_s / 3600, 3),
                "observed_two_exchange_hours": round(m.observed_two_s / 3600, 3),
                "coverage_pct": round(100 * m.observed_any_s / m.seconds, 4) if m.seconds else 0.0,
                "trading_status": "observed" if observed else "not observed (no order-book data)",
            })
        realised = sum((j.pnl_gbp for j in jobs if j.done), ZERO)
        fees = sum((j.fees_gbp for j in jobs), ZERO)
        slippage = sum((j.slippage_gbp for j in jobs if j.slippage_gbp > 0), ZERO)
        hedge = sum((j.hedge_loss_gbp for j in jobs), ZERO)
        rebal = self.core.rebalance_costs_gbp
        contributions = self.cum_deposits
        net_trading = realised - rebal
        market_pnl = final_value - contributions + self.conversion_costs - realised + rebal
        twr = time_weighted_return(self.points + [ValuePoint(cfg.end, final_value)])
        flows = [(datetime.fromisoformat(d["ts"]), -Decimal(d["amount_gbp"])) for d in self.deposit_log]
        irr = xirr(flows + [(cfg.end, final_value)])
        # drawdown on the contribution-neutral (time-weighted) index
        idx, peak, mdd = Decimal(1), Decimal(1), ZERO
        prev = None
        for p in self.points:
            if prev is not None and prev > 0:
                idx *= (p.value - p.flow) / prev
            prev = p.value
            peak = max(peak, idx)
            if peak > 0:
                mdd = max(mdd, (peak - idx) / peak * 100)
        bd = compute_metrics(initial_value=contributions or Decimal(1), final_value=final_value,
                             curve=[(p.ts, p.value) for p in self.points], jobs=jobs, counters=self.core.counters,
                             rejection_counts=self.core.rejection_counts, risk_rejections=self.core.risk_rejections,
                             exposure_samples=[], final_exposure=ZERO, rebalance_costs_gbp=rebal,
                             rebalances=self.core.rebalances)
        coverage_pct = 100 * observed_any / total_seconds if total_seconds else 0.0
        summary = {
            "variant": self.name,
            "initial_investment_gbp": q(cfg.plan.initial_amount),
            "monthly_contributions_gbp": q(contributions - cfg.plan.initial_amount),
            "contribution_count": sum(1 for d in self.deposit_log if d["label"] != "initial"),
            "total_contributions_gbp": q(contributions),
            "final_value_gbp": q(final_value),
            "final_valuation": src, "final_valuation_estimated": est,
            "final_valuation_basis": self.marks.price_at("BTC/GBP", cfg.end)[1],
            "net_gain_gbp": q(final_value - contributions),
            "trading": {
                "gross_trading_profit_gbp": q(realised + fees),
                "fees_gbp": q(fees), "slippage_gbp": q(slippage), "failed_hedge_losses_gbp": q(hedge),
                "rebalancing_costs_gbp": q(rebal), "net_trading_profit_gbp": q(net_trading),
                "trades": len(jobs),
                "successful": sum(1 for j in jobs if j.status == JobStatus.SUCCESS),
                "failed": sum(1 for j in jobs if j.status == JobStatus.FAILED),
                "partial": sum(1 for j in jobs if j.status in (JobStatus.PARTIAL_HEDGED, JobStatus.PARTIAL_HELD)),
                "avg_trade_pnl_gbp": q(realised / len(jobs)) if jobs else None,
                "opportunity_evaluations": self.core.counters.get("evaluations", 0),
                "opportunities_detected": self.core.counters.get("detected", 0),
                "opportunities_eligible": self.core.counters.get("eligible", 0),
            },
            "conversion_costs_gbp": q(self.conversion_costs),
            "market_inventory_pnl_gbp": q(market_pnl),
            "time_weighted_return_pct": q(twr * 100) if twr is not None else None,
            "money_weighted_return_annual_pct": round(irr * 100, 4) if irr is not None else None,
            "max_drawdown_twr_pct": q(mdd),
            "observed_trading_hours": round(observed_any / 3600, 3),
            "observed_two_exchange_hours": round(observed_two / 3600, 3),
            "period_hours": round(total_seconds / 3600, 1),
            "trading_coverage_pct": round(coverage_pct, 5),
            "result_completeness": "complete" if coverage_pct >= 99.0 else "partial",
        }
        breakdown = {k: bd[k] for k in ("pnl_by_exchange", "pnl_by_pair", "pnl_by_day", "rejection_reasons",
                                        "risk_rejections", "fill_ratio", "avg_net_margin_pct", "rebalances")}
        res = VariantResult(self.name, summary, monthly, self.equity, breakdown, self.deposit_log, jobs,
                            list(dict.fromkeys(self.warnings)))
        payload = {"summary": summary, "monthly": monthly,
                   "trades": [(j.job_id, j.status.value, str(j.pnl_gbp), [f.to_dict() for f in j.fills]) for j in jobs]}
        res.result_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
        return res


# ============================================================================ benchmarks
def cash_benchmark(cfg: SimulationConfig) -> dict:
    sched = cfg.plan.schedule(cfg.start, cfg.end)
    cum = ZERO
    monthly: dict[str, dict] = {}
    for ts, amt, _l in sched:
        cum += amt
        m = monthly.setdefault(month_key(ts), {"month": month_key(ts), "deposits_gbp": ZERO})
        m["deposits_gbp"] += amt
    rows, run = [], ZERO
    for k in sorted(monthly):
        run += monthly[k]["deposits_gbp"]
        rows.append({"month": k, "deposits_gbp": q(monthly[k]["deposits_gbp"]), "ending_value_gbp": q(run)})
    return {"label": "A: cash (no trading, no interest)", "final_value_gbp": q(cum), "total_contributions_gbp": q(cum),
            "net_gain_gbp": "0E-8", "time_weighted_return_pct": "0E-8",
            "money_weighted_return_annual_pct": 0.0, "monthly": rows, "market_risk": "none (nominal GBP)"}


def btc_benchmark(cfg: SimulationConfig, marks: DailyMarks, fees: FeeSchedule) -> dict:
    """Buy BTC with every contribution at the latest completed daily close on the benchmark exchange,
    paying that exchange's dated retail taker fee plus a spread allowance."""
    sched = cfg.plan.schedule(cfg.start, cfg.end)
    btc = ZERO
    cum = ZERO
    fees_paid = ZERO
    buys, monthly, points = [], {}, []
    spread = cfg.btc_benchmark_spread_bps / Decimal(10000)
    for ts, amt, label in sched:
        px, basis = marks.price_at("BTC/GBP", ts)
        fe = fees.at(cfg.btc_benchmark_exchange, ts)
        fee_rate = fe.taker if fe else Decimal("0.004")
        if px is None:
            buys.append({"ts": ts.isoformat(), "amount_gbp": q(amt), "status": "no price - kept as cash"})
            cum += amt
            continue
        exec_px = px * (1 + spread)
        fee = amt * fee_rate
        got = ((amt - fee) / exec_px).quantize(Q8)
        btc += got
        cum += amt
        fees_paid += fee
        buys.append({"ts": ts.isoformat(), "amount_gbp": q(amt), "price_gbp": q(px), "price_basis": basis,
                     "execution_price_gbp": q(exec_px), "fee_gbp": q(fee), "btc": str(got), "label": label})
        m = monthly.setdefault(month_key(ts), {"deposits": ZERO, "fees": ZERO})
        m["deposits"] += amt
        m["fees"] += fee
        v = btc * px + (cum - sum(Decimal(b["amount_gbp"]) for b in buys if "btc" in b))
        points.append(ValuePoint(ts, v, amt))
    end_px, end_basis = marks.price_at("BTC/GBP", cfg.end)
    final = btc * end_px if end_px is not None else None
    rows = []
    for k in sorted(monthly):
        y, mo = map(int, k.split("-"))
        me = min(datetime(y + (mo == 12), 1 if mo == 12 else mo + 1, 1, tzinfo=UTC), cfg.end)
        held = sum((Decimal(b["btc"]) for b in buys if "btc" in b and b["ts"] < me.isoformat()), ZERO)
        p, _ = marks.price_at("BTC/GBP", me)
        rows.append({"month": k, "deposits_gbp": q(monthly[k]["deposits"]), "fees_gbp": q(monthly[k]["fees"]),
                     "btc_held": str(held), "ending_value_gbp": q(held * p) if p is not None else None})
    daily = []
    d = cfg.start + timedelta(days=1)
    while d <= cfg.end:
        p, _ = marks.price_at("BTC/GBP", d)
        held = sum((Decimal(b["btc"]) for b in buys if "btc" in b and b["ts"] < d.isoformat()), ZERO)
        c = sum((Decimal(b["amount_gbp"]) for b in buys if b["ts"] < d.isoformat()), ZERO)
        if p is not None:
            daily.append({"ts": d.isoformat(), "value_gbp": q(held * p), "contributions_gbp": q(c)})
        d += timedelta(days=7)
    twr = time_weighted_return(points + [ValuePoint(cfg.end, final)]) if final is not None else None
    irr = xirr([(datetime.fromisoformat(b["ts"]), -Decimal(b["amount_gbp"])) for b in buys] + [(cfg.end, final)]) \
        if final is not None else None
    return {"label": "B: Bitcoin buy and hold", "exchange": cfg.btc_benchmark_exchange,
            "final_value_gbp": q(final) if final is not None else None, "final_price_basis": end_basis,
            "btc_held": str(btc), "total_contributions_gbp": q(cum), "fees_gbp": q(fees_paid),
            "net_gain_gbp": q(final - cum) if final is not None else None,
            "time_weighted_return_pct": q(twr * 100) if twr is not None else None,
            "money_weighted_return_annual_pct": round(irr * 100, 4) if irr is not None else None,
            "monthly": rows, "buys": buys, "equity_weekly": daily,
            "method": "Each contribution buys BTC at 00:00 UTC on its date using the previous day's completed daily "
                      f"close on {cfg.btc_benchmark_exchange} plus {cfg.btc_benchmark_spread_bps} bps spread allowance "
                      "and the dated retail taker fee. Valued at the latest completed daily close.",
            "market_risk": "full Bitcoin price exposure"}
