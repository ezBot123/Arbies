from datetime import timedelta
from decimal import Decimal as D

import pytest

from arbies.arbitrage.cross_exchange import CrossExchangeDetector
from arbies.arbitrage.models import StrategyParams
from arbies.arbitrage.triangular import TriangularDetector
from arbies.core.models import Side
from arbies.execution.portfolio import InsufficientBalance, Portfolio
from arbies.execution.simulator import (
    ArbitrageExecutor,
    ExecutionSimulator,
    FillModel,
    HedgePolicy,
    JobStatus,
    OrderStatus,
)
from arbies.risk.engine import RiskEngine, RiskLimits, RiskViolation
from tests.fixtures import T0, book, rules, ts, valuator


def funded() -> Portfolio:
    pf = Portfolio()
    pf.deposit(T0, "kraken", "USD", D("300"))
    pf.deposit(T0, "kraken", "BTC", D("0.002"))
    pf.deposit(T0, "coinbase", "USD", D("300"))
    pf.deposit(T0, "coinbase", "BTC", D("0.002"))
    return pf


# ----------------------------------------------------------------------- portfolio
def test_portfolio_cannot_spend_unavailable_balance():
    pf = funded()
    with pytest.raises(InsufficientBalance):
        pf.apply_trade(T0, "kraken", "BTC/USD", Side.BUY, D("1"), D("50000"), D("0"), "USD")
    assert pf.balance("kraken", "USD") == D("300")  # unchanged (atomic)


def test_portfolio_trade_and_fee_accounting():
    pf = funded()
    pf.apply_trade(T0, "kraken", "BTC/USD", Side.BUY, D("0.001"), D("50"), D("0.05"), "USD")
    assert pf.balance("kraken", "USD") == D("249.95")
    assert pf.balance("kraken", "BTC") == D("0.003")
    kinds = [e.kind for e in pf.ledger]
    assert "fee" in kinds


def test_portfolio_transfer_in_transit():
    pf = funded()
    pf.start_transfer(T0, "BTC", "kraken", "coinbase", D("0.001"), D("0.0001"), ts(60_000))
    assert pf.balance("kraken", "BTC") == D("0.0009")
    assert pf.settle_transfers(ts(30_000)) == 0
    assert pf.settle_transfers(ts(60_000)) == 1
    assert pf.balance("coinbase", "BTC") == D("0.003")


def test_portfolio_valuation():
    pf = funded()
    total, est = pf.total_value(valuator())
    # USD 600 * 0.8 + BTC 0.004 * 50000 * 0.8
    assert total == D("480") + D("160")
    assert not est


# ----------------------------------------------------------------------- risk
def _opp(fee="0.001", bid="50500", age_ts=T0, now=T0):
    r = {("kraken", "BTC/USD"): rules("kraken", "BTC/USD", fee), ("coinbase", "BTC/USD"): rules("coinbase", "BTC/USD", fee)}
    det = CrossExchangeDetector(StrategyParams(), r, valuator())
    b = {
        ("kraken", "BTC/USD"): book("kraken", "BTC/USD", [["49990", "1"]], [["50000", "1"]], ts=age_ts),
        ("coinbase", "BTC/USD"): book("coinbase", "BTC/USD", [[bid, "1"]], [["50510", "1"]], ts=age_ts),
    }
    return det.evaluate(b, now)[0], b


def test_risk_approves_qualified_opportunity():
    opp, _ = _opp()
    eng = RiskEngine(RiskLimits())
    d = eng.check(opp, funded(), valuator(), T0, {"kraken": True, "coinbase": True})
    assert d.approved, d.violations


def test_risk_kill_switch_blocks_everything():
    opp, _ = _opp()
    eng = RiskEngine()
    eng.activate_kill_switch(T0, "test")
    d = eng.check(opp, funded(), valuator(), T0)
    assert not d.approved and RiskViolation.KILL_SWITCH in d.violations


def test_risk_position_size_and_min_profit():
    opp, _ = _opp()
    eng = RiskEngine(RiskLimits(max_position_gbp=D("10"), min_net_return_pct=D("1")))
    d = eng.check(opp, funded(), valuator(), T0)
    assert RiskViolation.POSITION_SIZE in d.violations
    assert RiskViolation.MIN_PROFIT in d.violations


def test_risk_stale_data_and_unhealthy_exchange():
    opp, _ = _opp(age_ts=T0, now=ts(9000))
    eng = RiskEngine()
    d = eng.check(opp, funded(), valuator(), ts(9000), {"kraken": True, "coinbase": False})
    assert RiskViolation.STALE_DATA in d.violations
    assert RiskViolation.EXCHANGE_UNHEALTHY in d.violations


def test_risk_duplicate_order_protection():
    opp, _ = _opp()
    eng = RiskEngine()
    assert eng.check(opp, funded(), valuator(), T0).approved
    d2 = eng.check(opp, funded(), valuator(), ts(500))
    assert RiskViolation.DUPLICATE in d2.violations
    assert eng.check(opp, funded(), valuator(), ts(5000)).approved
    assert eng.register_client_order_id("abc")
    assert not eng.register_client_order_id("abc")


def test_risk_insufficient_balance():
    opp, _ = _opp()
    pf = Portfolio()
    pf.deposit(T0, "kraken", "USD", D("1"))
    d = RiskEngine().check(opp, pf, valuator(), T0)
    assert RiskViolation.INSUFFICIENT_BALANCE in d.violations


def test_risk_daily_loss_and_circuit_breaker():
    opp, _ = _opp()
    eng = RiskEngine(RiskLimits(max_daily_loss_gbp=D("5"), circuit_breaker_failures=2))
    eng.record_execution(T0, False, D("-3"))
    eng.record_execution(T0, False, D("-3"))
    d = eng.check(opp, funded(), valuator(), ts(1))
    assert RiskViolation.DAILY_LOSS in d.violations
    assert RiskViolation.CIRCUIT_BREAKER in d.violations
    assert any(e.kind == "circuit_breaker" for e in eng.events)


def test_risk_exposure_limits():
    opp, _ = _opp()
    # kraken holds 0.002 BTC (=£80); buying 0.000625 more there raises it to £105
    eng = RiskEngine(RiskLimits(max_exchange_exposure_gbp=D("90")))
    d = eng.check(opp, funded(), valuator(), T0)
    assert RiskViolation.EXCHANGE_EXPOSURE in d.violations
    # total BTC exposure is unchanged by a matched cross trade, so total limit not hit
    assert RiskViolation.TOTAL_EXPOSURE not in d.violations
    exp = eng.exposures(funded(), valuator())
    assert exp["total"] == D("160")  # cash (USD) does not count as exposure


def test_live_limits_need_user_approval():
    eng = RiskEngine(mode="live")
    with pytest.raises(PermissionError):
        eng.update_limits(RiskLimits(max_position_gbp=D("1000")), approved_by_user=False, ts=T0)
    eng.update_limits(RiskLimits(max_position_gbp=D("30")), approved_by_user=True, ts=T0)
    assert eng.limits.max_position_gbp == D("30")


# ----------------------------------------------------------------------- simulator
def _run_job(opp, pf, get_book, model):
    sim = ExecutionSimulator(model)
    ex = ArbitrageExecutor(sim)
    job = ex.start(opp, pf, valuator(), T0)
    t = T0
    for _ in range(20):
        t = max(t, job.next_due)
        ex.step(job, pf, get_book, t)
        if job.done:
            break
    return job


def test_cross_execution_success_books_profit():
    opp, b = _opp()
    pf = funded()
    job = _run_job(opp, pf, lambda e, s: b.get((e, s)), FillModel(latency_jitter_ms=0))
    assert job.status == JobStatus.SUCCESS
    assert job.pnl_gbp > 0
    assert job.fees_gbp > 0
    # base inventory conserved across exchanges, USD increased
    assert pf.balance("kraken", "BTC") + pf.balance("coinbase", "BTC") == D("0.004")


def test_cross_partial_fill_is_hedged_and_loss_recorded():
    opp, b = _opp()
    pf = funded()
    # at execution time the sell side book has moved: only a fraction available within the limit
    moved = dict(b)
    moved[("coinbase", "BTC/USD")] = book("coinbase", "BTC/USD", [["50500", "0.0002"], ["49000", "1"]], [["50510", "1"]])
    job = _run_job(opp, pf, lambda e, s: moved.get((e, s)), FillModel(latency_jitter_ms=0))
    statuses = [f.status for f in job.fills]
    assert OrderStatus.PARTIAL in statuses
    assert job.status == JobStatus.PARTIAL_HEDGED
    assert any(f.purpose == "hedge" for f in job.fills)
    # inventory restored: no lingering base imbalance created by the job
    assert pf.balance("kraken", "BTC") + pf.balance("coinbase", "BTC") == D("0.004")


def test_cross_partial_fill_hold_policy_keeps_inventory():
    opp, b = _opp()
    pf = funded()
    moved = dict(b)
    moved[("coinbase", "BTC/USD")] = book("coinbase", "BTC/USD", [["40000", "1"]], [["50510", "1"]])
    job = _run_job(opp, pf, lambda e, s: moved.get((e, s)), FillModel(latency_jitter_ms=0, hedge_policy=HedgePolicy.HOLD))
    assert job.status == JobStatus.PARTIAL_HELD
    assert pf.balance("kraken", "BTC") + pf.balance("coinbase", "BTC") > D("0.004")


def test_execution_rejects_when_balance_insufficient():
    opp, b = _opp()
    pf = Portfolio()
    pf.deposit(T0, "kraken", "USD", D("300"))  # no BTC on coinbase to sell
    job = _run_job(opp, pf, lambda e, s: b.get((e, s)), FillModel(latency_jitter_ms=0))
    assert any(f.status == OrderStatus.REJECTED for f in job.fills)
    assert job.status in (JobStatus.PARTIAL_HEDGED, JobStatus.PARTIAL_HELD)
    for v in pf.balances.values():
        assert v >= 0


def test_triangular_execution_and_unwind_on_failure():
    books = {
        ("kraken", "BTC/USD"): book("kraken", "BTC/USD", [["49995", "10"]], [["50000", "10"]]),
        ("kraken", "ETH/USD"): book("kraken", "ETH/USD", [["2500", "100"]], [["2501", "100"]]),
        ("kraken", "ETH/BTC"): book("kraken", "ETH/BTC", [["0.0489", "100"]], [["0.049", "100"]]),
    }
    r = {("kraken", s): rules("kraken", s, "0.001") for s in ["BTC/USD", "ETH/USD", "ETH/BTC"]}
    det = TriangularDetector(StrategyParams(start_currencies=["USD"]), r, valuator())
    opp = next(o for o in det.evaluate(books, T0) if o.route == ["USD", "BTC", "ETH", "USD"])
    assert opp.qualified
    pf = Portfolio()
    pf.deposit(T0, "kraken", "USD", D("500"))
    job = _run_job(opp, pf, lambda e, s: books.get((e, s)), FillModel(latency_jitter_ms=0))
    assert job.status == JobStatus.SUCCESS
    assert pf.balance("kraken", "USD") > D("500")

    # failure: ETH/BTC liquidity vanishes before leg 2 -> BTC unwound back to USD
    pf2 = Portfolio()
    pf2.deposit(T0, "kraken", "USD", D("500"))
    broken = dict(books)
    broken[("kraken", "ETH/BTC")] = book("kraken", "ETH/BTC", [["0.04", "1"]], [["0.06", "100"]])
    job2 = _run_job(opp, pf2, lambda e, s: broken.get((e, s)), FillModel(latency_jitter_ms=0))
    assert job2.status == JobStatus.PARTIAL_HEDGED
    assert pf2.balance("kraken", "BTC") == 0
    assert job2.pnl_gbp < 0
    assert job2.hedge_loss_gbp > 0


def test_simulator_deterministic_with_seed():
    s1 = ExecutionSimulator(FillModel(seed=7))
    s2 = ExecutionSimulator(FillModel(seed=7))
    assert [s1.sample_latency() for _ in range(5)] == [s2.sample_latency() for _ in range(5)]
    assert s1.sample_latency() > timedelta(0)
