"""Validation of the historical investment simulator (the 12 requirements in BACKTESTING.md, "Historical
investment simulation - validation").

Order books here are small hand-built test inputs (labelled synthetic in the test only); the simulator itself
refuses synthetic sources. Reference prices are constant or simple step series so results can be checked exactly.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from arbies.core.models import DataSource, MarketRules, OrderBook
from arbies.historical.plan import ContributionPlan, FeeEntry, FeeSchedule
from arbies.historical.reference import DailyMarks
from arbies.historical.simulator import (
    SimulationConfig,
    VariantReplay,
    btc_benchmark,
    cash_benchmark,
    preset_models,
)

START = datetime(2025, 1, 1, tzinfo=UTC)
END = datetime(2025, 4, 15, 23, 59, 59, tzinfo=UTC)
TRADE_DAY = datetime(2025, 2, 10, 12, 0, tzinfo=UTC)


def marks(btc_gbp=lambda d: D(80), gbp_usd=D("1.25"), days=200):
    rows = {"BTC/GBP": [], "GBP/USD": []}
    for i in range(-5, days):
        d = START + timedelta(days=i)
        rows["BTC/GBP"].append(SimpleNamespace(ts=d, open=btc_gbp(d), close=btc_gbp(d)))
        rows["GBP/USD"].append(SimpleNamespace(ts=d, open=gbp_usd, close=gbp_usd))
    return DailyMarks(rows)


def fees(kraken=D("0.004"), coinbase=D("0.012")) -> FeeSchedule:
    return FeeSchedule(name="test", entries=[
        FeeEntry(exchange="kraken", effective_from=date(2024, 1, 1), maker=kraken, taker=kraken),
        FeeEntry(exchange="coinbase", effective_from=date(2024, 1, 1), maker=coinbase, taker=coinbase)])


def cfg(**kw) -> SimulationConfig:
    base = dict(start=START, end=END, exchanges=["kraken", "coinbase"], symbols=["BTC/USD"],
                plan=ContributionPlan(initial_amount=D(500), recurring_amount=D(100)), run_parameter_study=False,
                rebalance={"enabled": False})
    base.update(kw)
    return SimulationConfig(**base)


def rules():
    return {(ex, "BTC/USD"): MarketRules(ex, "BTC/USD", "BTC", "USD", min_cost=D("1"), amount_step=D("0.00000001"))
            for ex in ("kraken", "coinbase")}


def replay(c: SimulationConfig, fs: FeeSchedule | None = None, mk: DailyMarks | None = None, name="combined"):
    params, risk, fill = preset_models(c, "moderate")
    return VariantReplay(name, ["cross_exchange", "triangular"], c, params, risk, fill, fs or fees(), rules(), mk or marks())


def arb_books(t0: datetime, seconds: int = 60, gap: D = D("3")):
    """kraken ask 100, coinbase bid 100+gap (USD) every 0.5 s - a persistent cross-exchange gap."""
    out = []
    for i in range(seconds * 2):
        t = t0 + timedelta(milliseconds=500 * i)
        out.append(OrderBook.from_levels("kraken", "BTC/USD", [["99.9", "10"]], [["100", "10"]], received_ts=t,
                                         exchange_ts=t, source=DataSource.SYNTHETIC))
        t2 = t + timedelta(milliseconds=10)
        out.append(OrderBook.from_levels("coinbase", "BTC/USD", [[str(100 + gap), "10"]], [[str(100 + gap + D("0.1")), "10"]],
                                         received_ts=t2, exchange_ts=t2, source=DataSource.SYNTHETIC))
    return out


def run(rp: VariantReplay, books=()):
    for ob in books:
        rp.on_book(ob)
    return rp.finish()


# 1. recurring deposit calculations  /  2. deposits are not profit
def test_deposits_arrive_on_schedule_and_are_not_profit():
    c = cfg()
    r = run(replay(c))
    s = r.summary
    assert [d["ts"][:10] for d in r.deposits] == ["2025-01-01", "2025-02-01", "2025-03-01", "2025-04-01"]
    assert D(s["total_contributions_gbp"]) == D(800) and s["contribution_count"] == 3
    # constant prices, no trading: value = contributions - conversion costs, nothing counted as profit
    conv = D(s["conversion_costs_gbp"])
    assert conv == D(800) * D("0.005")  # only BTC/USD traded: every pound is converted (50% USD, 50% BTC)
    assert D(s["final_value_gbp"]) == pytest.approx(D(800) - conv, abs=D("0.0001"))
    assert D(s["trading"]["net_trading_profit_gbp"]) == 0
    assert abs(D(s["market_inventory_pnl_gbp"])) < D("0.0001")
    assert D(s["time_weighted_return_pct"]) <= 0  # deposits never show up as return
    m = {x["month"]: x for x in r.monthly}
    assert D(m["2025-02"]["deposits_gbp"]) == 100 and m["2025-03"]["trading_pnl_gbp"] is None


# 3. cash and crypto reconcile / 4. fees reduce value / 5. inventory never negative / 12. no fictional trades
def test_trading_reconciles_and_fees_reduce_value():
    c = cfg()
    cheap = run(replay(c, fees(D("0.001"), D("0.001"))), arb_books(TRADE_DAY))
    dear = run(replay(c, fees(D("0.004"), D("0.004"))), arb_books(TRADE_DAY))
    for r in (cheap, dear):
        s = r.summary
        assert s["trading"]["trades"] > 0
        # accounting identity: value change = deposits - conversion + trading - rebalancing + market
        lhs = D(s["final_value_gbp"]) - D(s["total_contributions_gbp"])
        rhs = (-D(s["conversion_costs_gbp"]) + D(s["trading"]["net_trading_profit_gbp"])
               + D(s["market_inventory_pnl_gbp"]))
        assert lhs == pytest.approx(rhs, abs=D("0.000001"))
        # no fictional trades: every trade happened inside the order-book window
        assert all(TRADE_DAY <= j.created <= TRADE_DAY + timedelta(seconds=61) for j in r.jobs)
        # monthly breakdown sums to the totals
        assert sum(D(x["trading_pnl_gbp"] or 0) for x in r.monthly) == pytest.approx(
            D(s["trading"]["net_trading_profit_gbp"]), abs=D("0.000001"))
    # fees reduce value: same trades, higher fees -> lower P&L by roughly the fee difference
    assert cheap.summary["trading"]["trades"] == dear.summary["trading"]["trades"]
    assert D(dear.summary["trading"]["fees_gbp"]) > D(cheap.summary["trading"]["fees_gbp"])
    assert D(dear.summary["final_value_gbp"]) < D(cheap.summary["final_value_gbp"])
    diff_pnl = D(cheap.summary["trading"]["net_trading_profit_gbp"]) - D(dear.summary["trading"]["net_trading_profit_gbp"])
    diff_fee = D(dear.summary["trading"]["fees_gbp"]) - D(cheap.summary["trading"]["fees_gbp"])
    assert diff_pnl == pytest.approx(diff_fee, rel=D("0.02"))


def test_inventory_never_negative():
    rp = replay(cfg())
    for ob in arb_books(TRADE_DAY, seconds=300):
        rp.on_book(ob)
        assert all(v >= 0 for v in rp.portfolio.balances.values())
    rp.finish()
    assert all(v >= 0 for v in rp.portfolio.balances.values())


def test_fee_schedule_change_is_applied_on_its_date():
    fs = FeeSchedule(name="t", entries=[
        FeeEntry(exchange="kraken", effective_from=date(2024, 1, 1), maker=D("0.001"), taker=D("0.001")),
        FeeEntry(exchange="kraken", effective_from=date(2025, 2, 10), maker=D("0.004"), taker=D("0.008")),
        FeeEntry(exchange="coinbase", effective_from=date(2024, 1, 1), maker=D("0.001"), taker=D("0.001"))])
    rp = replay(cfg(), fs)
    assert rp.params.fee_overrides["kraken"] == D("0.001")
    rp.advance(datetime(2025, 2, 10, 0, 0, 1, tzinfo=UTC), inclusive=True)
    assert rp.params.fee_overrides["kraken"] == D("0.008") and rp.rules[("kraken", "BTC/USD")].maker_fee == D("0.004")


# 6. reproducible
def test_repeated_runs_are_identical():
    a = run(replay(cfg()), arb_books(TRADE_DAY))
    b = run(replay(cfg()), arb_books(TRADE_DAY))
    assert a.result_hash == b.result_hash and a.summary == b.summary


# 7. chronological processing
def test_out_of_order_events_are_rejected():
    rp = replay(cfg())
    books = arb_books(TRADE_DAY, seconds=2)
    rp.on_book(books[2])
    with pytest.raises(ValueError, match="chronological"):
        rp.on_book(books[0])


# 8. no future data
def test_reference_prices_never_look_ahead():
    mk = marks(btc_gbp=lambda d: D(80) + d.day)  # a different close every day
    day = datetime(2025, 2, 10, tzinfo=UTC)
    p, basis = mk.price_at("BTC/GBP", day + timedelta(hours=12))
    assert p == D(80 + 9) and basis.startswith("2025-02-09")  # previous completed day, never today's close
    p, _ = mk.price_at("BTC/GBP", day + timedelta(days=1))
    assert p == D(80 + 10)
    # deposits are priced with what was known at the deposit time
    rp = replay(cfg(), mk=mk)
    rp.finish()
    feb = [d for d in rp.deposit_log if d["ts"].startswith("2025-02-01")][0]
    btc_leg = [leg for leg in feb["legs"] if leg["asset"] == "BTC"][0]
    assert D(btc_leg["price_gbp"]) == D(80 + 31)  # 31 January close


# 9. equivalent contribution schedules across strategies and benchmarks
def test_benchmarks_use_identical_contribution_schedule():
    c = cfg()
    r = run(replay(c))
    cash = cash_benchmark(c)
    btc = btc_benchmark(c, marks(), fees())
    sched = [d["ts"] for d in r.deposits]
    assert [b["ts"] for b in btc["buys"]] == sched
    assert D(cash["final_value_gbp"]) == D(r.summary["total_contributions_gbp"]) == D(btc["total_contributions_gbp"])
    # BTC benchmark: constant price -> value = contributions minus fees and spread
    assert D(btc["final_value_gbp"]) < D(btc["total_contributions_gbp"])
    assert D(btc["fees_gbp"]) == D(800) * D("0.004")


# 10. incomplete data is not treated as continuous
def test_sparse_data_is_reported_as_partial():
    r = run(replay(cfg()), arb_books(TRADE_DAY, seconds=30))
    s = r.summary
    assert s["result_completeness"] == "partial" and s["trading_coverage_pct"] < 0.1
    observed = [m for m in r.monthly if m["trading_status"] == "observed"]
    assert [m["month"] for m in observed] == ["2025-02"]
    assert all(m["trading_pnl_gbp"] is None for m in r.monthly if m["month"] != "2025-02")


def test_simulator_refuses_synthetic_sources():
    with pytest.raises(ValueError, match="synthetic"):
        cfg(data_sources=[DataSource.SYNTHETIC])
