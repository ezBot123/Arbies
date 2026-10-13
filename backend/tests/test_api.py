"""API tests via ASGI transport (no network)."""

import httpx
import pytest

from arbies.api.app import create_app
from arbies.services.runtime import Runtime
from tests.conftest import T_END, T_START, feed_synthetic


@pytest.fixture
async def client(db, settings):
    rt = Runtime(settings, db=db)
    app = create_app(settings, runtime=rt, start_market_data=False)
    await rt.start(start_market_data=False)
    feed_synthetic(rt.hub)
    rt.scanner.scan()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c, rt
    await rt.stop()


async def login(c):
    r = await c.post("/api/auth/login", json={"username": "admin", "password": "test-password-1"})
    assert r.status_code == 200
    c.headers["Authorization"] = f"Bearer {r.json()['token']}"


async def test_auth_required(client):
    c, _ = client
    assert (await c.get("/api/health")).status_code == 200
    assert (await c.get("/api/system/status")).status_code == 401
    assert (await c.post("/api/auth/login", json={"username": "admin", "password": "wrong"})).status_code == 401
    await login(c)
    assert (await c.get("/api/system/status")).status_code == 200


async def test_login_rate_limited(client):
    c, _ = client
    codes = [(await c.post("/api/auth/login", json={"username": "admin", "password": "x"})).status_code
             for _ in range(12)]
    assert 429 in codes


async def test_core_endpoints(client):
    c, rt = client
    await login(c)
    st = (await c.get("/api/system/status")).json()
    assert st["operating_mode"] == "paper" and st["live_armed"] is False
    ex = (await c.get("/api/exchanges")).json()
    assert {e["exchange"] for e in ex} == {"kraken", "coinbase"}
    obs = (await c.get("/api/orderbooks?depth=3")).json()
    assert obs and len(obs[0]["bids"]) <= 3
    opp = (await c.get("/api/opportunities?include_no_spread=true")).json()
    assert opp["evaluated"] > 0 and "explanations" in opp
    for path in ["/api/strategies", "/api/agents", "/api/risk", "/api/settings", "/api/portfolio", "/api/paper",
                 "/api/backtests", "/api/walkforward", "/api/data/availability", "/api/agents/usage",
                 "/api/system/events", "/api/live/status", "/api/reports/summary?period=weekly"]:
        r = await c.get(path)
        assert r.status_code == 200, (path, r.text)
    settings = (await c.get("/api/settings")).json()
    assert "anthropic_api_key" not in str(settings) and "secret_key" not in settings["settings"]


async def test_backtest_flow_via_api(client):
    c, rt = client
    await login(c)
    r = await c.post("/api/data/synthetic", json={"exchanges": ["kraken", "coinbase"], "symbols": ["BTC/USD", "ETH/USD"],
                                                 "start": T_START.isoformat(), "end": T_END.isoformat(), "seed": 9})
    assert r.status_code == 200 and r.json()["snapshots"] > 0
    body = {"exchanges": ["kraken", "coinbase"], "symbols": ["BTC/USD", "ETH/USD"], "start": T_START.isoformat(),
            "end": T_END.isoformat(), "data_source": "synthetic"}
    rid = (await c.post("/api/backtests", json=body)).json()["id"]
    import asyncio
    for _ in range(100):
        run = (await c.get(f"/api/backtests/{rid}")).json()
        if run["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.1)
    assert run["status"] == "completed", run["error"]
    assert (await c.get(f"/api/backtests/{rid}/trades")).status_code == 200
    csv = await c.get(f"/api/reports/csv?source=backtest&run_id={rid}")
    assert csv.status_code == 200 and csv.text.startswith("ts,kind,status")
    cmp = (await c.get(f"/api/backtests/compare?ids={rid},{rid}")).json()
    assert len(cmp) == 2


async def test_strategy_versioning(client):
    c, _ = client
    await login(c)
    r = await c.post("/api/strategies", json={"name": "Test", "kind": "cross_exchange",
                                              "params": {"strategy": {"min_net_profit_pct": "0.5"}}})
    assert r.status_code == 200 and r.json()["version"] == 1
    r = await c.put("/api/strategies/Test", json={"params": {"strategy": {"min_net_profit_pct": "0.7"}}})
    assert r.json()["version"] == 2
    r = await c.post("/api/strategies/Test/restore/1")
    assert r.json()["version"] == 3 and r.json()["params"]["strategy"]["min_net_profit_pct"] == "0.5"
    vers = (await c.get("/api/strategies/Test/versions")).json()
    assert [v["version"] for v in vers] == [3, 2, 1]
    bad = await c.post("/api/strategies", json={"name": "Bad", "kind": "magic", "params": {}})
    assert bad.status_code == 400


async def test_live_arming_refused_and_kill_switch(client):
    c, rt = client
    await login(c)
    p = (await c.post("/api/live/preview", json={"exchanges": ["kraken"]})).json()
    r = await c.post("/api/live/arm", json={"preview_id": p["preview_id"], "confirmation_phrase": p["confirmation_phrase"],
                                            "password": "test-password-1"})
    assert r.status_code == 403 and "disabled" in r.json()["detail"]
    assert rt.gate.is_armed() is False
    r = await c.post("/api/risk/kill-switch", json={"active": True, "reason": "test"})
    assert r.json()["kill_switch"] is True
    r = await c.post("/api/risk/kill-switch", json={"active": False})
    assert r.status_code == 403  # password needed to release
    r = await c.post("/api/risk/kill-switch", json={"active": False, "password": "test-password-1"})
    assert r.json()["kill_switch"] is False
    audit = (await c.get("/api/system/audit")).json()
    assert {"live_arm_refused", "kill_switch_on", "kill_switch_off"} <= {a["action"] for a in audit}


async def test_agent_run_via_api(client):
    c, _ = client
    await login(c)
    r = await c.post("/api/agents/system_health/run", json={})
    assert r.status_code == 200 and r.json()["status"] == "completed"
    recs = (await c.get("/api/agents/recommendations")).json()
    if recs:
        d = await c.post(f"/api/agents/recommendations/{recs[0]['id']}", json={"decision": "rejected"})
        assert d.json()["status"] == "rejected"
    assert (await c.post("/api/agents/nope/run", json={})).status_code == 404


async def test_paper_via_api(client):
    c, rt = client
    await login(c)
    r = await c.post("/api/paper/start")
    assert r.status_code == 200 and r.json()["status"] == "running"
    assert (await c.post("/api/paper/pause")).json()["status"] == "paused"
    assert (await c.post("/api/paper/stop")).json()["status"] == "stopped"
    assert (await c.get("/api/paper/sessions")).json()
