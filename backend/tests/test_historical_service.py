"""Historical simulation service: persistence, API-facing views and CSV exports agree with each other
(requirement 11), and missing reference data fails loudly instead of being invented.

Rows inserted here are test fixtures in a throw-away database, tagged with provider 'test-fixture'."""

import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import func, insert, select

from arbies.core.models import DataSource, MarketRules
from arbies.db.models import HistoricalDataset, HistoricalSimTrade, OrderBookSnapshot, ReferencePrice
from arbies.historical.plan import ContributionPlan
from arbies.historical.service import HistoricalService
from arbies.historical.simulator import SimulationConfig

START = datetime(2025, 1, 1, tzinfo=UTC)
END = datetime(2025, 3, 10, tzinfo=UTC)
T = datetime(2025, 2, 3, 9, 0, tzinfo=UTC)


async def rules_for(exchanges, symbols):
    return {(e, s): MarketRules(e, s, "BTC", "USD", min_cost=D(1)) for e in exchanges for s in symbols}, []


async def seed(db, with_refs=True):
    async with db.session() as s:
        if with_refs:
            rows = []
            for i in range(-5, 80):
                d = START + timedelta(days=i)
                for sym, px in (("BTC/GBP", D(80)), ("GBP/USD", D("1.25"))):
                    rows.append({"exchange": "kraken", "symbol": sym, "interval": "1d", "ts": d, "open": px, "high": px,
                                 "low": px, "close": px, "volume": D(1), "source_url": "test-fixture"})
            await s.execute(insert(ReferencePrice), rows)
        ds = HistoricalDataset(provider="test-fixture", data_type="book_snapshot_1", exchange="kraken", symbol="BTC/USD",
                               file_name="fixture", sha256="0" * 64, status="completed")
        s.add(ds)
        await s.flush()
        books = []
        for i in range(120):
            t = T + timedelta(milliseconds=500 * i)
            for ex, bid, ask in (("kraken", "99.9", "100"), ("coinbase", "103", "103.1")):
                books.append({"exchange": ex, "symbol": "BTC/USD", "source": DataSource.IMPORTED.value, "received_ts": t,
                              "exchange_ts": t, "best_bid": D(bid), "best_ask": D(ask), "bids": [[bid, "10"]],
                              "asks": [[ask, "10"]], "depth": 1, "quality": {}, "dataset_id": ds.id})
        await s.execute(insert(OrderBookSnapshot), books)


def config():
    return SimulationConfig(start=START, end=END, exchanges=["kraken", "coinbase"], symbols=["BTC/USD"],
                            plan=ContributionPlan(), strategies=["cross_exchange", "combined"],
                            rebalance={"enabled": False}, run_parameter_study=True)


async def test_persisted_results_views_and_csv_agree(db):
    await seed(db)
    svc = HistoricalService(db, rules_for, "test")
    d = await svc.run_and_wait(config())
    assert d["status"] == "completed", d["error"]
    res = d["result"]
    head = res["headline"]
    assert head["answerable"] is False and head["status"].startswith("PARTIAL")
    assert head["total_personal_contributions_gbp"] == "700" and head["contribution_count"] == 2
    v = res["variants"][head["primary_variant"]]
    n_trades = v["summary"]["trading"]["trades"]
    assert n_trades > 0
    async with db.session() as s:
        stored = (await s.execute(select(func.count()).select_from(HistoricalSimTrade).where(
            HistoricalSimTrade.variant == head["primary_variant"]))).scalar_one()
    assert stored == n_trades
    # reloading gives the same numbers the dashboard charts are drawn from
    again = await svc.get(d["id"])
    assert again["result"]["variants"][head["primary_variant"]]["equity"] == v["equity"]
    monthly = list(csv.DictReader(io.StringIO(await svc.csv(d["id"], "monthly"))))
    assert [r["month"] for r in monthly] == [m["month"] for m in v["monthly"]]
    assert [r["ending_value_gbp"] for r in monthly] == [m["ending_value_gbp"] for m in v["monthly"]]
    trades_csv = list(csv.DictReader(io.StringIO(await svc.csv(d["id"], "trades", head["primary_variant"]))))
    assert len(trades_csv) == n_trades
    assert sum(D(r["pnl_gbp"]) for r in trades_csv) == pytest.approx(
        sum(D(m["trading_pnl_gbp"] or 0) for m in v["monthly"]), abs=D("0.000001"))
    eq = list(csv.DictReader(io.StringIO(await svc.csv(d["id"], "equity"))))
    assert [r["value_gbp"] for r in eq] == [e["value_gbp"] for e in v["equity"]]
    summary = list(csv.DictReader(io.StringIO(await svc.csv(d["id"], "summary"))))
    assert {r["variant"] for r in summary} >= {head["primary_variant"], "benchmark:cash", "benchmark:btc"}
    # benchmarks share the contribution schedule
    assert res["benchmarks"]["cash"]["final_value_gbp"] == "700.00000000"
    assert len(res["benchmarks"]["btc"]["buys"]) == 3
    # screening without minute candles reports missing data rather than results
    assert res["screening"]["pairs"] == [] and res["screening"]["missing"]
    # identical inputs -> identical result hash
    d2 = await svc.run_and_wait(config())
    assert d2["result_hash"] == d["result_hash"]


async def test_missing_reference_prices_fail_loudly(db):
    await seed(db, with_refs=False)
    d = await HistoricalService(db, rules_for, "test").run_and_wait(config())
    assert d["status"] == "failed" and "reference prices" in d["error"]


async def test_historical_agent_reads_results_but_test_window_is_sealed(db, settings):
    from arbies.agents.agents import AGENTS
    from arbies.agents.tools import ToolGateway, ToolPermissionError, registry
    from arbies.services.runtime import Runtime

    await seed(db)
    rt = Runtime(settings, db=db)
    rt.historical.rules_for = rules_for
    d = await rt.historical.run_and_wait(config())
    spec = AGENTS["historical_research"]
    gw = ToolGateway(registry, rt, "historical_research", spec.allowed_tools)
    sim = await gw.call("get_historical_simulation", {})
    assert sim["id"] == d["id"]
    assert all(not k.startswith("test_") for row in sim["parameter_study"]["rows"] for k in row)
    assert "chosen_test_pnl_gbp" not in sim["parameter_study"]
    report = await spec.analyze(gw, {})
    assert "PARTIAL" in report.summary and any("Obtain historical order-book data" in r.title for r in report.recommendations)
    with pytest.raises(ToolPermissionError):  # read-only: cannot run backtests or alter anything
        await gw.call("run_backtest", {})
    # results are unchanged by the agent run
    assert (await rt.historical.get(d["id"]))["result_hash"] == d["result_hash"]


async def test_parallel_workers_give_identical_results(db, tmp_path):
    await seed(db)
    seq = await HistoricalService(db, rules_for, "test", cache_dir=str(tmp_path / "a")).run_and_wait(config())
    par = await HistoricalService(db, rules_for, "test", workers=2, cache_dir=str(tmp_path / "b")).run_and_wait(config())
    assert par["status"] == "completed", par["error"]
    assert par["result_hash"] == seq["result_hash"]
    assert {k: v["result_hash"] for k, v in par["result"]["variants"].items()} == \
        {k: v["result_hash"] for k, v in seq["result"]["variants"].items()}
    assert not any((tmp_path / "b").rglob("*.pkl"))  # replay cache removed afterwards
