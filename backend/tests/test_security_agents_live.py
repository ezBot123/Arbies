"""Permission boundaries: agents cannot bypass risk controls or enable live trading; live trading gates."""

from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from arbies.agents.agents import AGENTS, AgentRunner
from arbies.agents.providers import AnthropicProvider
from arbies.agents.tools import Permission, ToolGateway, ToolPermissionError, ToolRegistry, registry
from arbies.core.models import MarketRules, Side
from arbies.execution.live import (
    LiveOrderManager,
    LiveOrderRequest,
    LiveTradingDisabled,
    LiveTradingGate,
    OrderValidationError,
    SafeExchange,
    reconcile_balances,
    validate_order,
)
from arbies.risk.engine import RiskEngine, RiskLimits
from arbies.security import CredentialVault, RateLimiter, hash_password, verify_password

PHRASE = "I UNDERSTAND THIS USES REAL MONEY"


# ------------------------------------------------------------------ agent tool permissions
def test_agent_tools_have_no_dangerous_capabilities():
    for name, tool in registry.tools.items():
        assert tool.permission in (Permission.READ, Permission.SIMULATE, Permission.PROPOSE)
        for frag in ("live", "order", "withdraw", "transfer", "credential", "secret", "kill", "risk_limit", "arm"):
            assert frag not in name, name


def test_registry_refuses_forbidden_tools():
    reg = ToolRegistry()
    for bad in ("enable_live_trading", "place_order", "withdraw_funds", "get_credentials", "release_kill_switch",
                "set_risk_limits", "transfer_funds"):
        with pytest.raises(ToolPermissionError):
            reg.register(bad, "x", Permission.READ)
    with pytest.raises(ToolPermissionError):
        reg.register("harmless", "x", "execute")  # type: ignore[arg-type]


def test_every_agent_allowlist_only_contains_registered_safe_tools():
    for spec in AGENTS.values():
        assert spec.allowed_tools <= set(registry.tools), spec.name
        if spec.name != "strategy_optimisation":
            # only the optimisation agent may run simulations
            assert not any(registry.tools[t].permission == Permission.SIMULATE for t in spec.allowed_tools)


async def test_gateway_denies_tools_outside_allowlist():
    gw = ToolGateway(registry, SimpleNamespace(), "system_health", AGENTS["system_health"].allowed_tools)
    with pytest.raises(ToolPermissionError):
        await gw.call("run_backtest", {"exchanges": ["kraken"], "symbols": ["BTC/USD"]})
    with pytest.raises(ToolPermissionError):
        await gw.call("enable_live_trading", {})
    assert gw.calls[-1].ok is False


class _FakeBlock(SimpleNamespace):
    pass


class _FakeMessages:
    """Simulates a model that first tries a forbidden tool, then an allowed one, then answers."""

    def __init__(self):
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        n = len(self.calls)
        usage = SimpleNamespace(input_tokens=100, output_tokens=50)
        if n == 1:
            return SimpleNamespace(stop_reason="tool_use", usage=usage, content=[
                _FakeBlock(type="tool_use", id="t1", name="enable_live_trading", input={}),
                _FakeBlock(type="tool_use", id="t2", name="get_system_status", input={})])
        return SimpleNamespace(stop_reason="end_turn", usage=usage,
                               content=[_FakeBlock(type="text", text="All good.")])


async def test_llm_tool_loop_cannot_escape_gateway():
    fake = SimpleNamespace(beta=SimpleNamespace(messages=_FakeMessages()))
    provider = AnthropicProvider("x", "claude-opus-5-5", client=fake)
    rt = SimpleNamespace(system_status=lambda: {"operating_mode": "paper"})
    gw = ToolGateway(registry, rt, "system_health", AGENTS["system_health"].allowed_tools)
    res = await provider.run("sys", "prompt", gw, max_tool_calls=5)
    assert res.text == "All good."
    second_request_msgs = fake.beta.messages.calls[1]["messages"]
    tool_results = next(m["content"] for m in second_request_msgs if m["role"] == "user" and isinstance(m["content"], list))
    denied = next(r for r in tool_results if r["tool_use_id"] == "t1")
    assert denied["is_error"] is True and "may not call" in denied["content"]
    ok = next(r for r in tool_results if r["tool_use_id"] == "t2")
    assert "paper" in ok["content"]
    # tools offered to the model never include anything outside the allowlist
    offered = {t["name"] for t in fake.beta.messages.calls[0]["tools"]}
    assert offered == AGENTS["system_health"].allowed_tools
    assert res.est_cost_usd > 0


async def test_agent_runner_deterministic_report_persisted(db, settings):
    from arbies.services.runtime import Runtime
    from tests.conftest import feed_synthetic
    rt = Runtime(settings, db=db)
    await rt.start(start_market_data=False)
    feed_synthetic(rt.hub)
    rt.scanner.scan()
    for name in ["system_health", "market_research", "risk_analysis", "performance_analyst", "strategy_optimisation",
                 "orchestrator"]:
        res = await rt.agents.run(name)
        assert res["status"] == "completed", (name, res["error"])
        assert res["report"]["summary"]
    tasks = await rt.agents.tasks()
    assert all(t["llm_used"] is False for t in tasks)
    assert any("No AI provider configured" in f for t in tasks for f in t["output"].get("findings", []))
    # agents never changed risk or live state
    assert rt.gate.is_armed() is False
    assert rt.global_kill is False
    await rt.stop()


async def test_agent_cannot_read_sealed_walkforward_test(db):
    from arbies.backtest.service import BacktestService
    svc = BacktestService(db, dict, {}, code_revision="t")
    rid = await svc.create_run(__import__("tests.test_services", fromlist=["cfg"]).cfg(), segment="test")
    rt = SimpleNamespace(backtests=svc)
    gw = ToolGateway(registry, rt, "strategy_optimisation", AGENTS["strategy_optimisation"].allowed_tools)
    res = await gw.call("get_backtest", {"id": rid})
    assert "sealed" in res["error"]
    assert await gw.call("list_backtests", {"limit": 10}) == []


# ------------------------------------------------------------------ live trading gates
def _rules():
    return MarketRules("kraken", "BTC/USD", "BTC", "USD", amount_step=D("0.0001"), price_step=D("0.1"),
                       min_amount=D("0.0001"), min_cost=D("5"))


def test_gate_disabled_by_environment():
    gate = LiveTradingGate(env_enabled=False, confirmation_phrase=PHRASE)
    p = gate.create_preview(["kraken"], {}, {}, {"kraken": "trade"})
    with pytest.raises(LiveTradingDisabled, match="disabled in the backend environment"):
        gate.arm(p["preview_id"], PHRASE, True, {"kraken": "trade"})
    assert not gate.is_armed()
    with pytest.raises(LiveTradingDisabled):
        gate.assert_can_trade("kraken")


def test_gate_requires_every_condition():
    gate = LiveTradingGate(env_enabled=True, confirmation_phrase=PHRASE)
    with pytest.raises(LiveTradingDisabled):
        gate.arm("nope", PHRASE, True, {"kraken": "trade"})  # no preview
    p = gate.create_preview(["kraken"], {}, {}, {"kraken": "read_only"})
    with pytest.raises(LiveTradingDisabled, match="password"):
        gate.arm(p["preview_id"], PHRASE, False, {"kraken": "trade"})
    with pytest.raises(LiveTradingDisabled, match="phrase"):
        gate.arm(p["preview_id"], "yes", True, {"kraken": "trade"})
    with pytest.raises(LiveTradingDisabled, match="trade-permission"):
        gate.arm(p["preview_id"], PHRASE, True, {"kraken": "read_only"})
    st = gate.arm(p["preview_id"], PHRASE, True, {"kraken": "trade"})
    assert st.armed and gate.is_armed("kraken") and not gate.is_armed("coinbase")
    gate.disarm()
    assert not gate.is_armed()


def test_safe_exchange_forbids_withdrawals_and_trading_when_read_only():
    calls = []
    fake = SimpleNamespace(withdraw=lambda *a: calls.append("w"), create_order=lambda *a: calls.append("o"),
                           fetch_balance=lambda: {"total": {}})
    ro = SafeExchange(fake, allow_trading=False)
    with pytest.raises(LiveTradingDisabled):
        ro.withdraw("BTC", 1, "addr")
    with pytest.raises(LiveTradingDisabled):
        ro.create_order("BTC/USD", "limit", "buy", 1, 1)
    with pytest.raises(LiveTradingDisabled):
        _ = ro.some_unlisted_method
    assert ro.fetch_balance() == {"total": {}}
    rw = SafeExchange(fake, allow_trading=True)
    with pytest.raises(LiveTradingDisabled):
        rw.withdraw("BTC", 1, "addr")
    with pytest.raises(LiveTradingDisabled):
        rw.transfer("BTC", 1, "a", "b")
    assert calls == []


def test_order_prevalidation():
    r = _rules()
    validate_order(LiveOrderRequest("kraken", "BTC/USD", Side.BUY, D("0.001"), D("50000.1")), r)
    with pytest.raises(OrderValidationError, match="lot size"):
        validate_order(LiveOrderRequest("kraken", "BTC/USD", Side.BUY, D("0.00015"), D("50000")), r)
    with pytest.raises(OrderValidationError, match="tick"):
        validate_order(LiveOrderRequest("kraken", "BTC/USD", Side.BUY, D("0.001"), D("50000.05")), r)
    with pytest.raises(OrderValidationError, match="minimum"):
        validate_order(LiveOrderRequest("kraken", "BTC/USD", Side.BUY, D("0.0001"), D("100")), r)
    with pytest.raises(OrderValidationError, match="notional"):
        validate_order(LiveOrderRequest("kraken", "BTC/USD", Side.BUY, D("0.01"), D("50000")), r, D("100"))
    with pytest.raises(OrderValidationError):
        validate_order(LiveOrderRequest("kraken", "BTC/USD", Side.BUY, D("0.001"), D("50000")), None)


class _FakeTradingExchange:
    def __init__(self):
        self.orders = []

    async def create_order(self, *a):
        self.orders.append(a)
        return {"id": f"x{len(self.orders)}", "status": "closed"}

    async def fetch_open_orders(self):
        return [{"id": "o1", "symbol": "BTC/USD"}]

    async def cancel_order(self, oid, sym):
        return {"id": oid, "status": "canceled"}


async def test_live_order_manager_enforces_gate_risk_and_idempotency():
    gate = LiveTradingGate(env_enabled=True, confirmation_phrase=PHRASE)
    risk = RiskEngine(RiskLimits(), mode="live")
    fake = _FakeTradingExchange()
    mgr = LiveOrderManager(gate, risk, {"kraken": SafeExchange(fake, allow_trading=True)}, {("kraken", "BTC/USD"): _rules()})
    req = LiveOrderRequest("kraken", "BTC/USD", Side.BUY, D("0.001"), D("50000.1"))
    with pytest.raises(LiveTradingDisabled):
        await mgr.submit(req)  # not armed
    p = gate.create_preview(["kraken"], {}, {}, {"kraken": "trade"})
    gate.arm(p["preview_id"], PHRASE, True, {"kraken": "trade"})
    res = await mgr.submit(req)
    assert res["id"] == "x1"
    assert fake.orders[0][-1]["cl_ord_id"] == req.client_order_id  # idempotency key passed to exchange
    with pytest.raises(OrderValidationError, match="duplicate"):
        await mgr.submit(req)
    risk.activate_kill_switch(__import__("arbies.core.models", fromlist=["utcnow"]).utcnow(), "test")
    with pytest.raises(LiveTradingDisabled, match="kill switch"):
        await mgr.submit(LiveOrderRequest("kraken", "BTC/USD", Side.BUY, D("0.001"), D("50000.1")))
    assert len(fake.orders) == 1
    out = await mgr.emergency_cancel_all()
    assert out["kraken"] == "cancelled 1"


def test_reconciliation_reports_differences():
    diffs = reconcile_balances({("kraken", "BTC"): D("1"), ("kraken", "USD"): D("100")},
                               {("kraken", "BTC"): D("0.9"), ("kraken", "USD"): D("100"), ("kraken", "ETH"): D("2")})
    assert {(d["asset"], d["difference"]) for d in diffs} == {("BTC", "-0.1"), ("ETH", "2")}


# ------------------------------------------------------------------ security primitives
def test_credential_vault_roundtrip_and_password_hashing():
    v = CredentialVault(CredentialVault.generate_key())
    blob = v.encrypt({"apiKey": "k", "secret": "s"})
    assert "secret" not in blob and v.decrypt(blob)["secret"] == "s"
    with pytest.raises(RuntimeError):
        CredentialVault(None).encrypt({})
    h = hash_password("pw")
    assert verify_password("pw", h) and not verify_password("nope", h)


def test_rate_limiter():
    rl = RateLimiter()
    assert all(rl.allow("k", 3, 60) for _ in range(3))
    assert not rl.allow("k", 3, 60)


def test_agent_runner_budget_type():
    assert isinstance(AgentRunner(SimpleNamespace(), None).monthly_budget, D)
