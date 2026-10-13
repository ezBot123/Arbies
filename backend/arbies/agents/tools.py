"""Permissioned tools available to AI agents.

Agents can only act through tools registered here. The permission model is
intentionally narrow:

* READ       - read system state and analytics
* SIMULATE   - run backtests / walk-forward optimisation (never touches money)
* PROPOSE    - store a recommendation for a human to approve or reject

There is deliberately no permission level for executing trades, enabling live
trading, changing risk limits, releasing the kill switch, reading credentials
or moving funds - and the registry refuses to register tools with names that
suggest those capabilities. All tool calls are validated, logged and attributed
to the calling agent.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import case, func, select

from arbies.core.models import DataSource, utcnow


class Permission(StrEnum):
    READ = "read"
    SIMULATE = "simulate"
    PROPOSE = "propose"


ALLOWED_PERMISSIONS = {Permission.READ, Permission.SIMULATE, Permission.PROPOSE}
FORBIDDEN_NAME_FRAGMENTS = ("live", "order", "withdraw", "transfer", "credential", "secret", "api_key",
                            "kill_switch", "set_risk", "update_risk", "enable", "arm", "execute_trade")


class ToolPermissionError(PermissionError):
    pass


@dataclass
class Tool:
    name: str
    description: str
    permission: Permission
    args_model: type[BaseModel]
    handler: Callable[..., Awaitable[Any]]

    def json_schema(self) -> dict:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return schema


class NoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, Tool] = {}

    def register(self, name: str, description: str, permission: Permission, args_model: type[BaseModel] = NoArgs):
        if permission not in ALLOWED_PERMISSIONS:
            raise ToolPermissionError(f"permission {permission} cannot be granted to agent tools")
        lowered = name.lower()
        if any(frag in lowered for frag in FORBIDDEN_NAME_FRAGMENTS):
            raise ToolPermissionError(f"tool name '{name}' suggests a forbidden capability")

        def deco(fn):
            self.tools[name] = Tool(name, description, permission, args_model, fn)
            return fn
        return deco


@dataclass
class ToolCallRecord:
    tool: str
    args: dict
    ok: bool
    summary: str
    ts: str = field(default_factory=lambda: utcnow().isoformat())


class ToolGateway:
    """The only path from an agent to the system. Enforces the agent's allowlist."""

    def __init__(self, registry: ToolRegistry, runtime, agent_name: str, allowed: set[str]):
        self.registry = registry
        self.rt = runtime
        self.agent = agent_name
        self.allowed = allowed
        self.calls: list[ToolCallRecord] = []

    def available_tools(self) -> list[Tool]:
        return [t for n, t in self.registry.tools.items() if n in self.allowed]

    async def call(self, name: str, args: dict | None = None) -> Any:
        args = args or {}
        if name not in self.allowed or name not in self.registry.tools:
            self.calls.append(ToolCallRecord(name, args, False, "denied: tool not permitted for this agent"))
            raise ToolPermissionError(f"agent '{self.agent}' may not call tool '{name}'")
        tool = self.registry.tools[name]
        try:
            parsed = tool.args_model(**args)
        except ValidationError as e:
            self.calls.append(ToolCallRecord(name, args, False, f"invalid arguments: {e.errors()[:3]}"))
            raise
        try:
            result = await tool.handler(self.rt, parsed, agent=self.agent)
        except Exception as e:  # noqa: BLE001
            self.calls.append(ToolCallRecord(name, args, False, f"error: {e}"[:300]))
            raise
        preview = json.dumps(result, default=str)
        self.calls.append(ToolCallRecord(name, args, True, preview[:300]))
        return result


registry = ToolRegistry()


# ============================================================================ READ tools
@registry.register("get_system_status", "Operating mode, market data mode, component status and uptime.", Permission.READ)
async def get_system_status(rt, args: NoArgs, agent: str):
    return rt.system_status()


@registry.register("get_exchange_health", "Connection health for each exchange: status, transport, stale symbols, "
                   "errors, reconnects, latency.", Permission.READ)
async def get_exchange_health(rt, args: NoArgs, agent: str):
    now = utcnow()
    return [h.to_dict(now, rt.settings.max_data_age_ms) for h in rt.hub.health().values()]


class OppArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(20, ge=1, le=100)
    qualified_only: bool = False


@registry.register("get_current_opportunities", "Currently evaluated arbitrage opportunities with costs and "
                   "rejection reasons (estimated, not realised, profit).", Permission.READ, OppArgs)
async def get_current_opportunities(rt, args: OppArgs, agent: str):
    now = utcnow()
    opps = [o for o in rt.scanner.current if (o.qualified or not args.qualified_only) and o.gross_spread_pct > 0]
    return [o.to_dict(now) for o in opps[: args.limit]]


class HoursArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hours: int = Field(24, ge=1, le=24 * 30)


@registry.register("get_opportunity_statistics", "Aggregated opportunity episodes per route over a lookback window: "
                   "episodes, qualified episodes, average/max estimated net return, by hour of day.",
                   Permission.READ, HoursArgs)
async def get_opportunity_statistics(rt, args: HoursArgs, agent: str):
    from arbies.db.models import OpportunityRecord as O
    since = utcnow() - timedelta(hours=args.hours)
    async with rt.db.session() as s:
        rows = (await s.execute(
            select(O.key, O.kind, func.count(), func.sum(case((O.qualified.is_(True), 1), else_=0)),
                   func.avg(O.net_return_pct), func.max(O.net_return_pct), func.avg(O.gross_spread_pct),
                   func.min(O.data_source))
            .where(O.detected_at >= since).group_by(O.key, O.kind)
            .order_by(func.max(O.net_return_pct).desc()).limit(50))).all()
        total = (await s.execute(select(func.count()).where(O.detected_at >= since))).scalar_one()
        qualified_rows = (await s.execute(select(O.detected_at).where(O.detected_at >= since, O.qualified.is_(True))
                                          .limit(20000))).scalars().all()
    by_hour: dict[int, int] = {}
    for t in qualified_rows:
        by_hour[t.hour] = by_hour.get(t.hour, 0) + 1
    return {
        "lookback_hours": args.hours,
        "records": total,
        "routes": [{"key": k, "kind": kind, "records": n, "qualified_records": int(q or 0),
                    "avg_net_return_pct": float(a) if a is not None else None,
                    "max_net_return_pct": float(m) if m is not None else None,
                    "avg_gross_spread_pct": float(g) if g is not None else None, "data_source": src}
                   for k, kind, n, q, a, m, g, src in rows],
        "qualified_by_hour_utc": dict(sorted(by_hour.items())),
        "note": "Records are opportunity episode starts and qualification changes, estimated before execution.",
    }


@registry.register("get_market_liquidity", "Per-market spread and depth (within 0.1% and 0.5% of top of book) "
                   "from the latest order books.", Permission.READ)
async def get_market_liquidity(rt, args: NoArgs, agent: str):
    from arbies.arbitrage.depth import depth_within
    now = utcnow()
    out = []
    for (ex, sym), ob in sorted(rt.hub.books.items()):
        if not ob.bids or not ob.asks:
            continue
        mid = ob.mid
        out.append({
            "exchange": ex, "symbol": sym, "age_ms": round(ob.age_ms(now)), "spread_pct": float(ob.spread_pct),
            "bid_depth_0_1pct": float(depth_within(ob.bids, ob.best_bid, Decimal("0.1"), False)),
            "ask_depth_0_1pct": float(depth_within(ob.asks, ob.best_ask, Decimal("0.1"), True)),
            "bid_depth_0_5pct": float(depth_within(ob.bids, ob.best_bid, Decimal("0.5"), False)),
            "ask_depth_0_5pct": float(depth_within(ob.asks, ob.best_ask, Decimal("0.5"), True)),
            "mid": float(mid) if mid else None, "source": ob.source.value,
        })
    return out


class LimitArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(10, ge=1, le=50)


@registry.register("list_backtests", "Recent backtest runs with headline metrics and data provenance.",
                   Permission.READ, LimitArgs)
async def list_backtests(rt, args: LimitArgs, agent: str):
    runs = await rt.backtests.list_runs(args.limit * 3)
    # never expose out-of-sample walk-forward test runs to agents
    return [r for r in runs if r.get("walkforward_segment") != "test"][: args.limit]


class IdArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int


@registry.register("get_backtest", "Full metrics of one backtest run (not including individual trades).",
                   Permission.READ, IdArgs)
async def get_backtest(rt, args: IdArgs, agent: str):
    r = await rt.backtests.get_run(args.id)
    if r is None:
        return {"error": "not found"}
    if r.get("walkforward_segment") == "test":
        return {"error": "out-of-sample test results are sealed from agents"}
    r.pop("equity_curve", None)
    return r


@registry.register("get_walkforward_study", "A walk-forward study's train/validation results. The out-of-sample "
                   "test result is always sealed for agents.", Permission.READ, IdArgs)
async def get_walkforward_study(rt, args: IdArgs, agent: str):
    return await rt.walkforward.get(args.id, include_test=False)


@registry.register("get_paper_trading_status", "Paper (simulated) trading session status, balances, exposure "
                   "and counters.", Permission.READ)
async def get_paper_trading_status(rt, args: NoArgs, agent: str):
    snap = rt.paper.snapshot()
    snap.pop("config", None)
    return snap


@registry.register("get_paper_trades", "Recent simulated paper trades including failures and hedges.",
                   Permission.READ, LimitArgs)
async def get_paper_trades(rt, args: LimitArgs, agent: str):
    trades = await rt.paper.trades(limit=args.limit)
    for t in trades:
        t["detail"] = {k: t["detail"].get(k) for k in ("status", "notes", "latency_ms")}
    return trades


@registry.register("get_risk_status", "Risk engine status: kill switch, circuit breaker, limits, daily P&L.",
                   Permission.READ)
async def get_risk_status(rt, args: NoArgs, agent: str):
    return rt.risk_status()


@registry.register("get_risk_events", "Recent risk events (circuit breakers, limit hits, kill switch).",
                   Permission.READ, LimitArgs)
async def get_risk_events(rt, args: LimitArgs, agent: str):
    from arbies.db.models import RiskEventRecord as R
    async with rt.db.session() as s:
        rows = (await s.execute(select(R).order_by(R.id.desc()).limit(args.limit))).scalars().all()
    return [{"ts": r.ts.isoformat(), "mode": r.mode, "kind": r.kind, "severity": r.severity, "message": r.message}
            for r in rows]


@registry.register("get_recent_errors", "Recent connection events/errors and system warnings.", Permission.READ,
                   LimitArgs)
async def get_recent_errors(rt, args: LimitArgs, agent: str):
    events = [e for e in list(rt.hub.events)[::-1] if e["event"] in ("error", "reconnecting", "fallback_rest", "resync",
                                                                     "disconnected")]
    return {"connection_events": events[: args.limit], "recorder": rt.recorder.status() if rt.recorder else None,
            "scanner": rt.scanner.stats}


@registry.register("get_data_availability", "Recorded historical data per source (live/imported/synthetic) "
                   "and market, with time coverage.", Permission.READ)
async def get_data_availability(rt, args: NoArgs, agent: str):
    from arbies.backtest.dataset import describe_available
    async with rt.db.session() as s:
        return await describe_available(s)


@registry.register("list_strategies", "Strategy configurations (current versions).", Permission.READ)
async def list_strategies(rt, args: NoArgs, agent: str):
    return await rt.strategies.list_current()


# ============================================================================ SIMULATE tools
class BacktestArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategies: list[str] = Field(default_factory=lambda: ["cross_exchange"])
    exchanges: list[str]
    symbols: list[str]
    hours_back: int = Field(24, ge=1, le=24 * 14)
    data_source: DataSource = DataSource.LIVE
    min_net_profit_pct: Decimal = Field(Decimal("0.30"), ge=Decimal("0"), le=Decimal("10"))
    slippage_buffer_bps: Decimal = Field(Decimal("5"), ge=Decimal("0"), le=Decimal("200"))


@registry.register("run_backtest", "Run a historical backtest on recorded data (simulation only). "
                   "Returns headline metrics.", Permission.SIMULATE, BacktestArgs)
async def run_backtest(rt, args: BacktestArgs, agent: str):
    from arbies.arbitrage.models import StrategyParams
    from arbies.backtest.config import BacktestConfig
    end = utcnow()
    cfg = BacktestConfig(name=f"agent:{agent}", strategies=args.strategies, exchanges=args.exchanges,
                         symbols=args.symbols, start=end - timedelta(hours=args.hours_back), end=end,
                         data_source=args.data_source,
                         params=StrategyParams(min_net_profit_pct=args.min_net_profit_pct,
                                               slippage_buffer_bps=args.slippage_buffer_bps))
    run_id, run = await rt.backtests.run_and_wait(cfg, requested_by=f"agent:{agent}")
    return {"run_id": run_id, "status": run["status"], "error": run["error"], "headline": run["headline"],
            "warnings": (run.get("metrics") or {}).get("warnings", [])}


class WalkForwardArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategies: list[str] = Field(default_factory=lambda: ["cross_exchange"])
    exchanges: list[str]
    symbols: list[str]
    hours_back: int = Field(72, ge=3, le=24 * 30)
    data_source: DataSource = DataSource.LIVE
    min_net_profit_pct_grid: list[Decimal] = Field(default_factory=lambda: [Decimal("0.2"), Decimal("0.3"), Decimal("0.5")])
    objective: str = "total_pnl_gbp"


@registry.register("run_walkforward_optimisation", "Create a walk-forward study and optimise on train/validation "
                   "windows. Selection is locked automatically; the out-of-sample test can only be run by the user.",
                   Permission.SIMULATE, WalkForwardArgs)
async def run_walkforward_optimisation(rt, args: WalkForwardArgs, agent: str):
    from arbies.backtest.config import BacktestConfig
    end = utcnow()
    base = BacktestConfig(name=f"agent:{agent} walk-forward", strategies=args.strategies, exchanges=args.exchanges,
                          symbols=args.symbols, start=end - timedelta(hours=args.hours_back), end=end,
                          data_source=args.data_source)
    sid = await rt.walkforward.create(base, {"min_net_profit_pct": args.min_net_profit_pct_grid},
                                      objective=args.objective, requested_by=f"agent:{agent}")
    return await rt.walkforward.optimise(sid)


# ============================================================================ PROPOSE tools
class RecommendationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=200)
    detail: str = Field(max_length=4000)
    severity: str = Field("info", pattern="^(info|warning|critical)$")
    proposed_change: dict | None = None


@registry.register("propose_recommendation", "Record a recommendation for the human operator to approve or reject. "
                   "Proposals are never applied automatically.", Permission.PROPOSE, RecommendationArgs)
async def propose_recommendation(rt, args: RecommendationArgs, agent: str):
    rid = await rt.agents.store_recommendation(agent, None, args.title, args.detail, args.severity,
                                               args.proposed_change)
    return {"recommendation_id": rid, "status": "open (awaiting human decision)"}
