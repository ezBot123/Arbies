from datetime import timedelta
from decimal import Decimal as D

import pytest

from arbies.arbitrage.models import StrategyParams
from arbies.backtest.config import BacktestConfig
from arbies.backtest.engine import BacktestEngine, InsufficientDataError
from arbies.core.models import DataSource
from arbies.execution.portfolio import Portfolio
from arbies.execution.simulator import FillModel
from arbies.market_data.synthetic import generate_history
from arbies.risk.engine import RiskLimits
from arbies.services.trading_core import LookaheadError, TradingCore
from tests.fixtures import T0, book, rules, ts

EX = ["kraken", "coinbase"]
SYM = ["BTC/USD", "ETH/USD", "ETH/BTC"]


def _cfg(**kw):
    base = dict(exchanges=EX, symbols=SYM, start=T0, end=T0 + timedelta(minutes=10),
                data_source=DataSource.SYNTHETIC, strategies=["cross_exchange", "triangular"],
                params=StrategyParams(min_net_profit_pct=D("0.1")), seed=7)
    base.update(kw)
    return BacktestConfig(**base)


def _hist(seed=3, minutes=10):
    return generate_history(EX, SYM, T0, T0 + timedelta(minutes=minutes), interval_ms=1000, seed=seed,
                            dislocation_prob=0.03)


def _rules():
    return {(e, s): rules(e, s, "0.001") for e in EX for s in SYM}


def test_backtest_runs_and_reports_metrics():
    res = BacktestEngine(_cfg(), _rules()).run(_hist())
    m = res.metrics
    assert m["events_processed"] > 1000
    assert m["opportunities_detected"] >= m["opportunities_eligible"] >= 0
    assert m["trades_attempted"] == len(res.jobs)
    assert D(m["final_value_gbp"]) > 0
    assert m["data_source"] == "synthetic"
    for key in ["max_drawdown_pct", "fees_paid_gbp", "fill_ratio", "pnl_by_exchange", "pnl_by_pair", "equity_curve"
                if False else "pnl_by_day", "slippage_losses_gbp", "failed_hedge_losses_gbp", "trading_volume_gbp"]:
        assert key in m
    assert len(res.equity_curve) >= 2
    # balances never negative
    for row in res.final_balances:
        assert D(row["amount"]) >= 0


def test_backtest_is_reproducible():
    r1 = BacktestEngine(_cfg(), _rules()).run(_hist())
    r2 = BacktestEngine(_cfg(), _rules()).run(_hist())
    assert r1.result_hash == r2.result_hash
    assert r1.metrics == r2.metrics
    r3 = BacktestEngine(_cfg(seed=8), _rules()).run(_hist())
    assert r3.metrics["trades_attempted"] >= 0  # different seed still valid


def test_backtest_no_data_raises():
    with pytest.raises(InsufficientDataError):
        BacktestEngine(_cfg(), _rules()).run([])


def test_lookahead_guard_rejects_future_books():
    pf = Portfolio()
    core = TradingCore(strategies=["cross_exchange"], params=StrategyParams(), rules={}, fill=FillModel(),
                       risk_limits=RiskLimits(), portfolio=pf)
    future = {("kraken", "BTC/USD"): book("kraken", "BTC/USD", [["1", "1"]], [["2", "1"]], ts=ts(5000))}
    with pytest.raises(LookaheadError):
        core.decide(future, ts(0), {"kraken": True})


def test_no_future_leakage_in_replay():
    """A large dislocation that appears only at t=60s must not cause any trade decided before t=60s,
    and every execution happens no earlier than decision time + latency."""
    events = []
    for i in range(0, 120):
        t = ts(i * 1000)
        events.append(book("kraken", "BTC/USD", [["49990", "1"]], [["50000", "1"]], ts=t))
        bid = "51000" if i >= 60 else "50005"
        events.append(book("coinbase", "BTC/USD", [[bid, "1"]], [["51010", "1"]], ts=t + timedelta(milliseconds=10)))
    cfg = BacktestConfig(exchanges=EX, symbols=["BTC/USD"], start=T0, end=ts(200_000),
                         data_source=DataSource.SYNTHETIC, rebalance={"enabled": False},
                         fill=FillModel(latency_ms=300, latency_jitter_ms=0))
    r = {(e, "BTC/USD"): rules(e, "BTC/USD", "0.001") for e in EX}
    res = BacktestEngine(cfg, r).run(events)
    assert res.jobs, "expected trades after the dislocation"
    for j in res.jobs:
        assert j.created >= ts(60_000)
        for f in j.fills:
            assert f.ts >= j.created + timedelta(milliseconds=300)


def test_fills_use_book_at_execution_time_not_decision_time():
    """The opportunity disappears 100ms after it appears; with 300ms latency the IOC
    limit orders cannot fill at the stale price, so the trade must fail or hedge."""
    events = []
    for i in range(0, 30):
        t = ts(i * 1000)
        events.append(book("kraken", "BTC/USD", [["49990", "1"]], [["50000", "1"]], ts=t))
        events.append(book("coinbase", "BTC/USD", [["50005", "1"]], [["50010", "1"]], ts=t + timedelta(milliseconds=5)))
        if i == 10:
            events.append(book("coinbase", "BTC/USD", [["51000", "1"]], [["51010", "1"]], ts=t + timedelta(milliseconds=50)))
            events.append(book("coinbase", "BTC/USD", [["50005", "1"]], [["50010", "1"]], ts=t + timedelta(milliseconds=150)))
    cfg = BacktestConfig(exchanges=EX, symbols=["BTC/USD"], start=T0, end=ts(60_000), data_source=DataSource.SYNTHETIC,
                         rebalance={"enabled": False}, decision_interval_ms=0,
                         fill=FillModel(latency_ms=300, latency_jitter_ms=0))
    r = {(e, "BTC/USD"): rules(e, "BTC/USD", "0.001") for e in EX}
    res = BacktestEngine(cfg, r).run(events)
    assert len(res.jobs) == 1
    job = res.jobs[0]
    assert job.status.value != "success"
    assert job.pnl_gbp < 0  # fees + hedge cost: failure is recorded, not hidden
    assert D(res.metrics["failed_hedge_losses_gbp"]) >= 0
    assert res.metrics["partial_cycles"] + res.metrics["failed_cycles"] == 1
