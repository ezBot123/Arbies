"""Integration tests: persistence, backtest service, walk-forward sealing, recorder replay, paper trading."""

import asyncio
from datetime import timedelta
from decimal import Decimal as D

import pytest

from arbies.arbitrage.models import StrategyParams
from arbies.backtest.config import BacktestConfig
from arbies.backtest.dataset import load_books
from arbies.backtest.service import BacktestService, WalkForwardService
from arbies.core.models import DataSource
from arbies.market_data.hub import MarketDataHub
from arbies.market_data.recorder import MarketDataRecorder
from arbies.market_data.synthetic import generate_history
from arbies.paper.service import PaperTrader
from tests.conftest import T_END, T_START, feed_synthetic

EX = ["kraken", "coinbase"]
SYM = ["BTC/USD", "ETH/USD", "ETH/BTC"]


def cfg(**kw):
    base = dict(exchanges=EX, symbols=SYM, start=T_START, end=T_END, data_source=DataSource.SYNTHETIC,
                strategies=["cross_exchange", "triangular"], params=StrategyParams(min_net_profit_pct=D("0.1")))
    base.update(kw)
    return BacktestConfig(**base)


async def test_backtest_requires_data_and_reports_clearly(db):
    svc = BacktestService(db, dict, {"USD": D("0.8")}, code_revision="test")
    run_id, run = await svc.run_and_wait(cfg(data_source=DataSource.LIVE))
    assert run["status"] == "failed"
    assert "No live_recorded order-book data" in run["error"]


async def test_backtest_service_end_to_end_and_reproducible(db):
    svc = BacktestService(db, dict, {"USD": D("0.8")}, code_revision="test")
    gen = await svc.generate_synthetic_dataset(EX, SYM, T_START, T_END, 1000, seed=11, dislocation_prob=0.03)
    assert gen["snapshots"] > 0
    rid1, r1 = await svc.run_and_wait(cfg())
    rid2, r2 = await svc.run_and_wait(cfg())
    assert r1["status"] == "completed", r1["error"]
    assert r1["result_hash"] == r2["result_hash"]
    assert r1["dataset_id"] == r2["dataset_id"] and r1["dataset_id"]
    assert r1["code_revision"] == "test"
    assert "SYNTHETIC DATA: results say nothing about real market profitability" in r1["metrics"]["warnings"]
    trades = await svc.get_trades(rid1)
    assert len(trades) == r1["metrics"]["trades_attempted"]
    comp = await svc.compare([rid1, rid2])
    assert len(comp) == 2


async def test_walkforward_test_segment_sealed_until_locked(db):
    svc = BacktestService(db, dict, {"USD": D("0.8")}, code_revision="test")
    await svc.generate_synthetic_dataset(EX, SYM, T_START, T_END, 2000, seed=3, dislocation_prob=0.03)
    wf = WalkForwardService(db, svc)
    sid = await wf.create(cfg(), {"min_net_profit_pct": [D("0.1"), D("0.3")]}, top_k=2)
    with pytest.raises(PermissionError):
        await wf.run_test(sid)  # not locked yet
    res = await wf.optimise(sid)
    assert res["status"] == "locked"
    assert res["selected_params"] is not None
    assert res["test_result"] is None
    # no run from the test window exists before the user runs the test
    runs = await svc.list_runs()
    assert not [r for r in runs if r["walkforward_segment"] == "test"]
    with pytest.raises(PermissionError):
        await wf.optimise(sid)  # cannot re-optimise a locked study
    tested = await wf.run_test(sid)
    assert tested["status"] == "tested"
    assert tested["test_result"]["run_id"]
    sealed_view = await wf.get(sid, include_test=False)
    assert sealed_view["test_result"] == "sealed"
    with pytest.raises(PermissionError):
        await wf.run_test(sid)  # only once


async def test_recorder_persists_replayable_snapshots(db, settings):
    hub = MarketDataHub(settings)
    rec = MarketDataRecorder(hub, db, interval_ms=1000, depth=5)
    books = generate_history(EX, ["BTC/USD"], T_START, T_START + timedelta(seconds=20), 500, seed=1)
    accepted = sum(rec.offer(ob) for ob in books)
    assert 0 < accepted < len(books)  # throttled
    await rec.flush()
    loaded = await load_books(db, DataSource.SYNTHETIC, EX, ["BTC/USD"], T_START, T_START + timedelta(minutes=1))
    assert len(loaded) == accepted
    assert all(a.received_ts <= b.received_ts for a, b in zip(loaded, loaded[1:], strict=False))
    assert loaded[0].source == DataSource.SYNTHETIC
    assert len(loaded[0].bids) == 5
    # live-recorded query must not silently return synthetic data
    assert await load_books(db, DataSource.LIVE, EX, ["BTC/USD"], T_START, T_START + timedelta(minutes=1)) == []


async def test_paper_trader_lifecycle(db, settings):
    hub = MarketDataHub(settings)
    feed_synthetic(hub)
    pt = PaperTrader(db, hub, settings)
    with pytest.raises(RuntimeError):
        await PaperTrader(db, MarketDataHub(settings), settings).start()  # no market data
    snap = await pt.start()
    assert snap["status"] == "running" and snap["simulated"] is True
    assert D(snap["value_gbp"]) == pytest.approx(D("500"), rel=D("0.001"))
    for _ in range(5):
        feed_synthetic(hub)
        await asyncio.sleep(0.15)
    await pt.pause()
    assert pt.status == "paused"
    await pt.start()
    assert pt.status == "running"
    await pt.stop()
    assert pt.status == "stopped"
    sessions = await pt.sessions()
    assert sessions[0]["status"] == "stopped"
    # restore only picks up unfinished sessions
    pt2 = PaperTrader(db, hub, settings)
    await pt2.restore()
    assert pt2.status == "stopped"
    await pt.reset()
    assert pt.core is None


async def test_paper_session_not_restored_across_data_sources(db, settings):
    from arbies.config import MarketDataMode
    synth = settings.model_copy(update={"market_data_mode": MarketDataMode.SYNTHETIC})
    hub = MarketDataHub(synth)
    feed_synthetic(hub)
    pt = PaperTrader(db, hub, synth)
    await pt.start()
    await pt.pause()  # unfinished session recorded on synthetic data
    live = settings.model_copy(update={"market_data_mode": MarketDataMode.LIVE})
    pt2 = PaperTrader(db, MarketDataHub(live), live)
    await pt2.restore()
    assert pt2.core is None and pt2.status == "stopped"
    assert (await pt2.sessions())[0]["status"] == "stopped"
    # an empty hub reports no data source rather than claiming live data
    assert MarketDataHub(live).data_source() == "none"


async def test_running_paper_session_survives_graceful_restart(db, settings):
    hub = MarketDataHub(settings)
    feed_synthetic(hub)
    pt = PaperTrader(db, hub, settings)
    await pt.start()
    sid, balances = pt.session_id, pt.core.portfolio.snapshot()
    await pt.suspend()  # what Runtime.stop() does on shutdown
    assert (await pt.sessions())[0]["status"] == "paused"
    pt2 = PaperTrader(db, hub, settings)
    await pt2.restore()
    assert pt2.status == "paused" and pt2.session_id == sid
    assert pt2.core.portfolio.snapshot() == balances
    await pt2.start()  # resumes the same session rather than opening a new one
    assert pt2.status == "running" and pt2.session_id == sid
    await pt2.stop()
