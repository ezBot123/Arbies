"""Strategy pipeline shared by the backtester and the paper trader.

detection (deterministic) -> risk engine (deterministic) -> simulated execution

Using one implementation for both guarantees that paper trading and
backtesting apply identical strategy logic, risk rules and fill assumptions.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

from arbies.arbitrage.cross_exchange import CrossExchangeDetector
from arbies.arbitrage.models import Opportunity, StrategyParams
from arbies.arbitrage.triangular import TriangularDetector
from arbies.backtest.config import RebalanceConfig
from arbies.core.models import ZERO, MarketRules, OrderBook
from arbies.core.valuation import Valuator
from arbies.execution.portfolio import Portfolio
from arbies.execution.simulator import (
    ArbitrageExecutor,
    ExecutionJob,
    ExecutionSimulator,
    FillModel,
    JobStatus,
)
from arbies.risk.engine import RiskEngine, RiskLimits


class LookaheadError(RuntimeError):
    """Raised if a decision would use data from the future (backtest integrity guard)."""


class TradingCore:
    def __init__(self, *, strategies: list[str], params: StrategyParams, rules: dict[tuple[str, str], MarketRules],
                 fill: FillModel, risk_limits: RiskLimits, portfolio: Portfolio, reporting: str = "GBP",
                 fallback_fx: dict[str, Decimal] | None = None, mode: str = "paper",
                 max_concurrent_jobs_per_exchange: int = 1, rebalance: RebalanceConfig | None = None,
                 on_job_done: Callable[[ExecutionJob], None] | None = None, keep_jobs: bool = True):
        self.params = params
        self.rules = rules
        self.portfolio = portfolio
        self.valuator = Valuator(reporting, fallback_fx)
        self.detectors = []
        if "cross_exchange" in strategies:
            self.detectors.append(CrossExchangeDetector(params, rules, self.valuator))
        if "triangular" in strategies:
            self.detectors.append(TriangularDetector(params, rules, self.valuator))
        self.risk = RiskEngine(risk_limits, mode)

        def fee_lookup(ex: str, sym: str) -> Decimal:
            r = rules.get((ex, sym))
            return params.taker_fee(ex, sym, r.taker_fee if r else Decimal("0.0026"))

        self.sim = ExecutionSimulator(fill, rules, fee_lookup=fee_lookup)
        self.executor = ArbitrageExecutor(self.sim, reporting)
        self.max_jobs_per_ex = max_concurrent_jobs_per_exchange
        self.rebalance_cfg = rebalance
        self.on_job_done = on_job_done
        self.keep_jobs = keep_jobs
        self.active_jobs: list[ExecutionJob] = []
        self.completed_jobs: list[ExecutionJob] = []
        self.counters = {"evaluations": 0, "detected": 0, "eligible": 0, "attempted": 0}
        self.rejection_counts: dict[str, int] = defaultdict(int)
        self.risk_rejections: dict[str, int] = defaultdict(int)
        self._episode_last: dict[tuple[str, str], datetime] = {}
        self.episode_gap = timedelta(seconds=5)
        self.last_opportunities: list[Opportunity] = []
        self.first_seen: dict[str, datetime] = {}
        self.rebalance_costs_gbp = ZERO
        self.rebalances = 0
        self._last_rebalance_check: datetime | None = None
        self.decisions = 0

    # ------------------------------------------------------------------ helpers
    def _new_episode(self, category: str, key: str, now: datetime) -> bool:
        k = (category, key)
        last = self._episode_last.get(k)
        self._episode_last[k] = now
        return last is None or now - last > self.episode_gap

    def update_valuation(self, books: dict[tuple[str, str], OrderBook]) -> None:
        self.valuator.update_from_books(books.values())

    # ------------------------------------------------------------------ decide
    def decide(self, books: dict[tuple[str, str], OrderBook], now: datetime,
               health_map: dict[str, bool]) -> list[Opportunity]:
        for ob in books.values():
            if ob.received_ts > now:
                raise LookaheadError(f"book {ob.exchange} {ob.symbol} at {ob.received_ts} used at {now}")
        self.decisions += 1
        self.update_valuation(books)

        def balances(ex: str, asset: str) -> Decimal:
            return self.portfolio.available(ex, asset)

        def health(ex: str) -> bool:
            return health_map.get(ex, False)

        opps: list[Opportunity] = []
        for det in self.detectors:
            opps.extend(det.evaluate(books, now, balances, health))
        live_keys = set()
        for o in opps:
            self.counters["evaluations"] += 1
            live_keys.add(o.key)
            if o.gross_spread_pct > 0:
                fs = self.first_seen.setdefault(o.key, now)
                o.first_seen = fs
                if self._new_episode("detected", o.key, now):
                    self.counters["detected"] += 1
                    for r in o.rejection_reasons:
                        self.rejection_counts[r.value] += 1
                if o.qualified and self._new_episode("eligible", o.key, now):
                    self.counters["eligible"] += 1
            else:
                self.first_seen.pop(o.key, None)
        for k in list(self.first_seen):
            if k not in live_keys:
                del self.first_seen[k]

        busy: dict[str, int] = defaultdict(int)
        for j in self.active_jobs:
            for ex in j.opp.exchanges:
                busy[ex] += 1
        qualified = sorted((o for o in opps if o.qualified), key=lambda o: o.net_profit_gbp or ZERO, reverse=True)
        for o in qualified:
            if any(busy[ex] >= self.max_jobs_per_ex for ex in o.exchanges):
                continue
            decision = self.risk.check(o, self.portfolio, self.valuator, now, health_map)
            if not decision.approved:
                for v in decision.violations:
                    self.risk_rejections[v.value] += 1
                continue
            job = self.executor.start(o, self.portfolio, self.valuator, now)
            self.risk.open_order_exposure_gbp += o.notional_gbp or ZERO
            self.active_jobs.append(job)
            self.counters["attempted"] += 1
            for ex in o.exchanges:
                busy[ex] += 1
        self.last_opportunities = opps
        return opps

    # ------------------------------------------------------------------ execute
    def next_due(self) -> datetime | None:
        return min((j.next_due for j in self.active_jobs), default=None)

    def process_jobs(self, get_book: Callable[[str, str], OrderBook | None], now: datetime) -> list[ExecutionJob]:
        finished = []
        for job in list(self.active_jobs):
            guard = 0
            while not job.done and job.next_due <= now and guard < 10:
                self.executor.step(job, self.portfolio, get_book, job.next_due)
                guard += 1
            if job.done:
                self.active_jobs.remove(job)
                self.risk.open_order_exposure_gbp = max(ZERO, self.risk.open_order_exposure_gbp - (job.opp.notional_gbp or ZERO))
                self.risk.record_execution(now, job.status == JobStatus.SUCCESS, job.pnl_gbp)
                if self.keep_jobs:
                    self.completed_jobs.append(job)
                finished.append(job)
                if self.on_job_done:
                    self.on_job_done(job)
        return finished

    # ------------------------------------------------------------------ rebalance
    def maybe_rebalance(self, now: datetime, exchanges: list[str]) -> int:
        cfg = self.rebalance_cfg
        if cfg is None or not cfg.enabled or len(exchanges) < 2:
            return 0
        if self._last_rebalance_check and (now - self._last_rebalance_check).total_seconds() < cfg.check_interval_s:
            return 0
        self._last_rebalance_check = now
        self.portfolio.settle_transfers(now)
        moved = 0
        for asset in sorted(self.portfolio.assets()):
            bals = {ex: self.portfolio.balance(ex, asset) for ex in exchanges}
            total = sum(bals.values(), ZERO)
            if total <= 0:
                continue
            hi = max(bals, key=lambda e: bals[e])
            lo = min(bals, key=lambda e: bals[e])
            if bals[hi] / total * 100 <= cfg.threshold_pct:
                continue
            target_hi = total * cfg.target_pct / 100
            amount = bals[hi] - target_hi
            fee = cfg.withdrawal_fees.get(asset, ZERO) + amount * cfg.transfer_fee_bps / Decimal(10000)
            if amount <= 0 or fee * 10 > amount or bals[hi] < amount + fee:
                continue  # not worth moving / cannot afford
            arrival = now + timedelta(seconds=cfg.transfer_delay_s)
            self.portfolio.start_transfer(now, asset, hi, lo, amount, fee, arrival)
            self.rebalance_costs_gbp += self.valuator.to_reporting(asset, fee)
            self.rebalances += 1
            moved += 1
        return moved
