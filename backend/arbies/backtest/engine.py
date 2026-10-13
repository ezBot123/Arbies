"""Event-driven historical replay engine for arbitrage strategies.

Integrity rules:
* Events are replayed strictly in local-receive-time order (``received_ts``),
  the moment Arbies would actually have known about each update. This
  preserves cross-exchange timing relationships as observed.
* Decisions only ever see books whose ``received_ts <= now`` (enforced by
  ``TradingCore.decide`` which raises ``LookaheadError`` otherwise).
* Orders execute at decision time + simulated latency against whatever book
  state exists at that later time - never the book the decision used.
* Randomness (latency jitter, maker fills) comes from a seeded RNG, so a run
  is fully reproducible from its config + dataset.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from arbies.backtest.config import BacktestConfig
from arbies.backtest.metrics import compute_metrics
from arbies.core.models import MarketRules, OrderBook
from arbies.core.valuation import Valuator
from arbies.execution.portfolio import Portfolio
from arbies.execution.simulator import ExecutionJob
from arbies.services.trading_core import TradingCore


@dataclass
class BacktestResult:
    metrics: dict
    equity_curve: list[dict]
    jobs: list[ExecutionJob]
    final_balances: list[dict]
    initial_balances: list[dict]
    events_processed: int
    decisions: int
    warnings: list[str] = field(default_factory=list)
    result_hash: str = ""

    def trade_rows(self) -> list[dict]:
        return [j.to_dict() for j in self.jobs]


class InsufficientDataError(Exception):
    pass


def fund_portfolio(cfg: BacktestConfig, valuator: Valuator, ts: datetime) -> tuple[Portfolio, list[str]]:
    """Convert the GBP allocation into asset amounts at the first available prices."""
    pf = Portfolio()
    warnings = []
    for item in cfg.resolved_allocation():
        gbp = cfg.initial_capital_gbp * item.fraction
        amount = valuator.from_reporting(item.asset, gbp)
        if amount is None:
            warnings.append(f"cannot price {item.asset}; allocation to {item.exchange} skipped")
            continue
        r = valuator.rate(item.asset)
        if r is not None and r.estimated:
            warnings.append(f"{item.asset} funded using a static fallback FX rate")
        pf.deposit(ts, item.exchange, item.asset, amount.quantize(Decimal("0.00000001")))
    return pf, warnings


class BacktestEngine:
    def __init__(self, cfg: BacktestConfig, rules: dict[tuple[str, str], MarketRules],
                 fallback_fx: dict[str, Decimal] | None = None):
        self.cfg = cfg
        self.rules = rules
        if fallback_fx is None:
            from arbies.config import get_settings
            fallback_fx = get_settings().fallback_fx_to_gbp
        self.fallback_fx = fallback_fx

    def run(self, events: Iterable[OrderBook], total_events: int | None = None,
            progress: Callable[[float], None] | None = None) -> BacktestResult:
        cfg = self.cfg
        exchanges = set(cfg.exchanges)
        symbols = set(cfg.symbols)
        it = iter(events)
        books: dict[tuple[str, str], OrderBook] = {}
        warnings: list[str] = []

        # ---- warm-up: collect the first book of each market before funding the portfolio
        pending: list[OrderBook] = []
        needed = {(e, s) for e in exchanges for s in symbols}
        for ob in it:
            if ob.exchange not in exchanges or ob.symbol not in symbols:
                continue
            pending.append(ob)
            books[(ob.exchange, ob.symbol)] = ob
            if needed <= set(books) or (pending[-1].received_ts - pending[0].received_ts) > timedelta(minutes=5):
                break
        if not pending:
            raise InsufficientDataError("no market data in the selected period for these exchanges/symbols")
        missing = needed - set(books)
        if missing:
            warnings.append("no data for: " + ", ".join(f"{e} {s}" for e, s in sorted(missing)))
        start_ts = pending[-1].received_ts

        val = Valuator("GBP", self.fallback_fx)
        val.update_from_books(books.values())
        portfolio, w = fund_portfolio(cfg, val, start_ts)
        warnings += w
        core = TradingCore(strategies=cfg.strategies, params=cfg.params, rules=self.rules, fill=cfg.fill,
                           risk_limits=cfg.risk, portfolio=portfolio, fallback_fx=self.fallback_fx, mode="backtest",
                           max_concurrent_jobs_per_exchange=cfg.max_concurrent_jobs_per_exchange,
                           rebalance=cfg.rebalance)
        core.update_valuation(books)
        initial_value, est = portfolio.total_value(core.valuator)
        if est:
            warnings.append("initial valuation used fallback FX rates")
        initial_balances = portfolio.snapshot(core.valuator)

        decision_iv = timedelta(milliseconds=cfg.decision_interval_ms)
        equity_iv = timedelta(seconds=cfg.equity_sample_interval_s)
        curve: list[tuple[datetime, Decimal]] = [(start_ts, initial_value)]
        exposure_samples: list[Decimal] = []
        last_decision = start_ts - decision_iv
        last_equity = start_ts
        n = len(pending)
        max_age = cfg.params.max_data_age_ms

        def get_book(ex: str, sym: str) -> OrderBook | None:
            return books.get((ex, sym))

        def health_map(now: datetime) -> dict[str, bool]:
            # an exchange is "healthy" in replay if it produced data recently (gaps = outages)
            out = {}
            for ex in exchanges:
                newest = max((ob.received_ts for (e, _s), ob in books.items() if e == ex), default=None)
                out[ex] = newest is not None and (now - newest).total_seconds() * 1000 <= max_age
            return out

        def handle(ob: OrderBook) -> None:
            nonlocal last_decision, last_equity
            now = ob.received_ts
            # 1) execute orders that became due before this event, against the then-current books
            while True:
                due = core.next_due()
                if due is None or due > now:
                    break
                core.process_jobs(get_book, due)
            # 2) apply the new market event
            books[(ob.exchange, ob.symbol)] = ob
            portfolio.settle_transfers(now)
            # 3) periodic decision
            if now - last_decision >= decision_iv:
                last_decision = now
                core.decide(books, now, health_map(now))
                core.maybe_rebalance(now, cfg.exchanges)
            # 4) equity sampling
            if now - last_equity >= equity_iv:
                last_equity = now
                core.update_valuation(books)
                v, _ = portfolio.total_value(core.valuator)
                curve.append((now, v))
                exposure_samples.append(core.risk.exposures(portfolio, core.valuator)["total"])

        # replay warm-up events after funding (decisions start at start_ts)
        for ob in pending:
            if ob.received_ts >= start_ts:
                handle(ob)
        end = cfg.end
        last_ts = start_ts
        for ob in it:
            if ob.exchange not in exchanges or ob.symbol not in symbols:
                continue
            if ob.received_ts > end:
                break
            if ob.received_ts < last_ts:
                raise ValueError("events are not in chronological order")
            last_ts = ob.received_ts
            handle(ob)
            n += 1
            if progress and total_events and n % 2000 == 0:
                progress(min(0.99, n / total_events))

        # drain pending jobs at their due times using the last known books
        guard = 0
        while core.active_jobs and guard < 100:
            due = core.next_due()
            core.process_jobs(get_book, due)
            guard += 1
        core.update_valuation(books)
        final_value, est2 = portfolio.total_value(core.valuator)
        if est2:
            warnings.append("final valuation used fallback FX rates")
        curve.append((last_ts, final_value))
        final_exposure = core.risk.exposures(portfolio, core.valuator)["total"]
        metrics = compute_metrics(initial_value=initial_value, final_value=final_value, curve=curve,
                                  jobs=core.completed_jobs, counters=core.counters,
                                  rejection_counts=core.rejection_counts, risk_rejections=core.risk_rejections,
                                  exposure_samples=exposure_samples, final_exposure=final_exposure,
                                  rebalance_costs_gbp=core.rebalance_costs_gbp, rebalances=core.rebalances)
        metrics["events_processed"] = n
        metrics["decisions"] = core.decisions
        metrics["period_start"] = start_ts.isoformat()
        metrics["period_end"] = last_ts.isoformat()
        metrics["data_source"] = cfg.data_source.value
        warnings = list(dict.fromkeys(warnings))
        metrics["warnings"] = warnings
        result = BacktestResult(
            metrics=metrics,
            equity_curve=[{"ts": t.isoformat(), "value_gbp": str(v.quantize(Decimal("0.00000001")))} for t, v in curve],
            jobs=core.completed_jobs,
            final_balances=portfolio.snapshot(core.valuator),
            initial_balances=initial_balances,
            events_processed=n,
            decisions=core.decisions,
            warnings=warnings,
        )
        result.result_hash = result_hash(result)
        if progress:
            progress(1.0)
        return result


def result_hash(result: BacktestResult) -> str:
    payload = {
        "metrics": {k: v for k, v in result.metrics.items()},
        "trades": [(j.job_id, j.status.value, str(j.pnl_gbp), [f.to_dict() for f in j.fills]) for j in result.jobs],
        "final": result.final_balances,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

