"""REST API routes."""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select

from arbies.agents.agents import AGENTS
from arbies.arbitrage.models import REJECTION_EXPLANATIONS, StrategyParams
from arbies.backtest.config import BacktestConfig
from arbies.backtest.dataset import describe_available
from arbies.core.models import utcnow
from arbies.db.models import (
    AuditLog,
    AuthSession,
    ConnectionEvent,
    OpportunityRecord,
    PaperTrade,
    RiskEventRecord,
    SettingKV,
    SystemLog,
)
from arbies.execution.live import LiveTradingDisabled
from arbies.paper.service import PaperConfig
from arbies.risk.engine import RiskLimits
from arbies.security import new_token, token_hash, verify_password

from .deps import COOKIE, get_rt, rate_limit, require_user, session_ttl

public = APIRouter(prefix="/api")
api = APIRouter(prefix="/api", dependencies=[Depends(require_user)])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


# ============================================================================ auth / health
class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


@public.get("/health")
async def health(rt=Depends(get_rt)):
    return {"status": "ok", "time": utcnow().isoformat(), "market_data_mode": rt.settings.market_data_mode.value}


@public.post("/auth/login", dependencies=[Depends(rate_limit("login", 10, 300))])
async def login(body: LoginBody, request: Request, response: Response, rt=Depends(get_rt)):
    ok = body.username == rt.settings.admin_username and verify_password(body.password, rt.admin_password_hash)
    await rt.audit(body.username if ok else "anonymous", "login" if ok else "login_failed", ip=_ip(request))
    if not ok:
        raise HTTPException(status_code=401, detail="invalid credentials")
    tok = new_token()
    async with rt.db.session() as s:
        s.add(AuthSession(token_hash=token_hash(tok), username=body.username, expires_at=utcnow() + session_ttl(rt),
                          ip=_ip(request)))
    response.set_cookie(COOKIE, tok, httponly=True, samesite="strict", secure=rt.settings.cookie_secure,
                        max_age=int(session_ttl(rt).total_seconds()))
    return {"username": body.username, "token": tok}


@public.post("/auth/logout")
async def logout(request: Request, response: Response, rt=Depends(get_rt)):
    tok = request.cookies.get(COOKIE)
    if tok:
        async with rt.db.session() as s:
            await s.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash(tok)))
    response.delete_cookie(COOKIE)
    return {"ok": True}


@api.get("/auth/me")
async def me(user: str = Depends(require_user)):
    return {"username": user}


# ============================================================================ system
@api.get("/system/status")
async def system_status(rt=Depends(get_rt)):
    return rt.system_status()


@api.get("/system/events")
async def system_events(limit: int = Query(100, le=500), rt=Depends(get_rt)):
    async with rt.db.session() as s:
        logs = (await s.execute(select(SystemLog).order_by(SystemLog.id.desc()).limit(limit))).scalars().all()
        conns = (await s.execute(select(ConnectionEvent).order_by(ConnectionEvent.id.desc()).limit(limit))).scalars().all()
        risks = (await s.execute(select(RiskEventRecord).order_by(RiskEventRecord.id.desc()).limit(limit))).scalars().all()
    events = ([{"ts": r.ts.isoformat(), "type": "system", "level": r.level, "source": r.component, "message": r.message}
               for r in logs]
              + [{"ts": r.ts.isoformat(), "type": "connection", "level": "warning" if r.event in ("error", "reconnecting", "resync") else "info",
                  "source": r.exchange + (f" {r.symbol}" if r.symbol else ""), "message": f"{r.event}: {r.detail}"}
                 for r in conns]
              + [{"ts": r.ts.isoformat(), "type": "risk", "level": r.severity, "source": f"risk ({r.mode})",
                  "message": r.message} for r in risks])
    # include in-memory hub events not yet flushed
    events.sort(key=lambda e: e["ts"], reverse=True)
    return events[:limit]


@api.get("/system/audit")
async def audit_log(limit: int = Query(100, le=1000), rt=Depends(get_rt)):
    async with rt.db.session() as s:
        rows = (await s.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))).scalars().all()
    return [{"ts": r.ts.isoformat(), "actor": r.actor, "action": r.action, "target": r.target, "detail": r.detail,
             "ip": r.ip} for r in rows]


# ============================================================================ market data
@api.get("/exchanges")
async def exchanges(rt=Depends(get_rt)):
    from arbies.market_data.hub import UK_ELIGIBILITY_NOTES
    now = utcnow()
    out = []
    for ex, h in rt.hub.health().items():
        d = h.to_dict(now, rt.settings.max_data_age_ms)
        d["uk_eligibility_note"] = UK_ELIGIBILITY_NOTES.get(ex, "Verify UK eligibility and API permissions.")
        d["fees"] = {sym: {"taker": str(r.taker_fee), "maker": str(r.maker_fee), "source": r.fee_source}
                     for (e, sym), r in rt.hub.rules.items() if e == ex}
        out.append(d)
    return out


@api.get("/orderbooks")
async def orderbooks(depth: int = Query(10, ge=1, le=50), exchange: str | None = None, symbol: str | None = None,
                     rt=Depends(get_rt)):
    now = utcnow()
    out = []
    for (ex, sym), ob in sorted(rt.hub.books.items()):
        if (exchange and ex != exchange) or (symbol and sym != symbol):
            continue
        d = ob.to_dict(depth)
        d["age_ms"] = round(ob.age_ms(now))
        d["spread_pct"] = str(ob.spread_pct) if ob.spread_pct is not None else None
        d["mid"] = str(ob.mid) if ob.mid is not None else None
        out.append(d)
    return out


@api.get("/data/availability")
async def data_availability(rt=Depends(get_rt)):
    async with rt.db.session() as s:
        return await describe_available(s)


class SyntheticBody(BaseModel):
    exchanges: list[str]
    symbols: list[str]
    start: datetime
    end: datetime
    interval_ms: int = Field(1000, ge=100, le=60000)
    seed: int = 1234
    dislocation_prob: float = Field(0.01, ge=0, le=0.2)


@api.post("/data/synthetic")
async def generate_synthetic(body: SyntheticBody, request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    try:
        res = await rt.backtests.generate_synthetic_dataset(body.exchanges, body.symbols, body.start, body.end,
                                                            body.interval_ms, body.seed, body.dislocation_prob)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await rt.audit(user, "generate_synthetic_dataset", detail=res, ip=_ip(request))
    return res


# ============================================================================ opportunities
@api.get("/opportunities")
async def opportunities(kind: str | None = None, qualified_only: bool = False, include_no_spread: bool = False,
                        limit: int = Query(200, le=1000), rt=Depends(get_rt)):
    now = utcnow()
    out = []
    for o in rt.scanner.current:
        if kind and o.kind.value != kind:
            continue
        if qualified_only and not o.qualified:
            continue
        if not include_no_spread and o.gross_spread_pct <= 0:
            continue
        out.append(o.to_dict(now))
        if len(out) >= limit:
            break
    return {"opportunities": out, "evaluated": len(rt.scanner.current), "stats": rt.scanner.stats,
            "data_source": rt.hub.data_source(), "params": rt.scanner.params.model_dump(mode="json"),
            "explanations": {k.value: v for k, v in REJECTION_EXPLANATIONS.items()}}


@api.get("/opportunities/history")
async def opportunity_history(hours: int = Query(24, ge=1, le=720), qualified_only: bool = False,
                              limit: int = Query(500, le=5000), rt=Depends(get_rt)):
    since = utcnow() - timedelta(hours=hours)
    async with rt.db.session() as s:
        q = select(OpportunityRecord).where(OpportunityRecord.detected_at >= since)
        if qualified_only:
            q = q.where(OpportunityRecord.qualified.is_(True))
        rows = (await s.execute(q.order_by(OpportunityRecord.id.desc()).limit(limit))).scalars().all()
        total = (await s.execute(select(func.count()).where(OpportunityRecord.detected_at >= since))).scalar_one()
        qual = (await s.execute(select(func.count()).where(OpportunityRecord.detected_at >= since,
                                                           OpportunityRecord.qualified.is_(True)))).scalar_one()
    return {"total": total, "qualified": qual, "records": [r.detail | {"record_id": r.id} for r in rows]}


# ============================================================================ backtests
@api.post("/backtests")
async def create_backtest(cfg: BacktestConfig, request: Request, strategy_name: str | None = None,
                          strategy_version: int | None = None, user: str = Depends(require_user), rt=Depends(get_rt)):
    if (cfg.end - cfg.start) > timedelta(days=31):
        raise HTTPException(400, "backtest period limited to 31 days")
    run_id = await rt.backtests.submit(cfg, requested_by=user, strategy_name=strategy_name,
                                       strategy_version=strategy_version)
    await rt.audit(user, "backtest_submit", str(run_id), {"strategies": cfg.strategies}, _ip(request))
    return {"id": run_id, "status": "queued"}


@api.get("/backtests")
async def list_backtests(rt=Depends(get_rt)):
    return await rt.backtests.list_runs()


@api.get("/backtests/compare")
async def compare_backtests(ids: str, rt=Depends(get_rt)):
    try:
        id_list = [int(x) for x in ids.split(",") if x.strip()][:8]
    except ValueError as e:
        raise HTTPException(400, "ids must be comma-separated integers") from e
    return await rt.backtests.compare(id_list)


@api.get("/backtests/{run_id}")
async def get_backtest(run_id: int, rt=Depends(get_rt)):
    r = await rt.backtests.get_run(run_id)
    if r is None:
        raise HTTPException(404, "not found")
    return r


@api.get("/backtests/{run_id}/trades")
async def backtest_trades(run_id: int, limit: int = Query(1000, le=10000), rt=Depends(get_rt)):
    return await rt.backtests.get_trades(run_id, limit)


@api.delete("/backtests/{run_id}")
async def delete_backtest(run_id: int, request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    ok = await rt.backtests.delete_run(run_id)
    await rt.audit(user, "backtest_delete", str(run_id), ip=_ip(request))
    return {"deleted": ok}


class WalkForwardBody(BaseModel):
    base: BacktestConfig
    param_grid: dict[str, list[Decimal]]
    train_frac: float = 0.6
    val_frac: float = 0.2
    objective: str = "total_pnl_gbp"
    top_k: int = Field(3, ge=1, le=10)


@api.post("/walkforward")
async def create_walkforward(body: WalkForwardBody, user: str = Depends(require_user), rt=Depends(get_rt)):
    import asyncio
    try:
        sid = await rt.walkforward.create(body.base, body.param_grid, body.train_frac, body.val_frac, body.objective,
                                          body.top_k, requested_by=user)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    async def go():
        try:
            await rt.walkforward.optimise(sid)
        except Exception:  # noqa: BLE001 - recorded on the study
            pass
    asyncio.create_task(go())
    return {"id": sid, "status": "optimising"}


@api.get("/walkforward")
async def list_walkforward(rt=Depends(get_rt)):
    return await rt.walkforward.list(include_test=True)


@api.get("/walkforward/{sid}")
async def get_walkforward(sid: int, rt=Depends(get_rt)):
    try:
        return await rt.walkforward.get(sid, include_test=True)
    except KeyError as e:
        raise HTTPException(404, "not found") from e


@api.post("/walkforward/{sid}/test")
async def run_walkforward_test(sid: int, request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    try:
        res = await rt.walkforward.run_test(sid)
    except PermissionError as e:
        raise HTTPException(409, str(e)) from e
    await rt.audit(user, "walkforward_test", str(sid), ip=_ip(request))
    return res


# ============================================================================ paper trading
@api.get("/paper")
async def paper_status(rt=Depends(get_rt)):
    snap = rt.paper.snapshot()
    if rt.paper.session_id is not None:
        async with rt.db.session() as s:
            row = (await s.execute(select(func.count(), func.coalesce(func.sum(PaperTrade.pnl_gbp), 0),
                                          func.coalesce(func.sum(PaperTrade.fees_gbp), 0),
                                          func.coalesce(func.sum(PaperTrade.hedge_loss_gbp), 0))
                                   .where(PaperTrade.session_id == rt.paper.session_id))).one()
        snap["persisted"] = {"trades": row[0], "simulated_realised_pnl_gbp": str(row[1]), "fees_gbp": str(row[2]),
                             "hedge_losses_gbp": str(row[3])}
    return snap


@api.post("/paper/start")
async def paper_start(request: Request, cfg: PaperConfig | None = None, user: str = Depends(require_user),
                      rt=Depends(get_rt)):
    try:
        res = await rt.paper.start(cfg)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    await rt.audit(user, "paper_start", str(rt.paper.session_id), ip=_ip(request))
    return res


@api.post("/paper/pause")
async def paper_pause(request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    await rt.audit(user, "paper_pause", str(rt.paper.session_id), ip=_ip(request))
    return await rt.paper.pause()


@api.post("/paper/stop")
async def paper_stop(request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    await rt.audit(user, "paper_stop", str(rt.paper.session_id), ip=_ip(request))
    return await rt.paper.stop()


@api.post("/paper/reset")
async def paper_reset(request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    await rt.audit(user, "paper_reset", str(rt.paper.session_id), ip=_ip(request))
    return await rt.paper.reset()


@api.get("/paper/trades")
async def paper_trades(session_id: int | None = None, limit: int = Query(500, le=5000), rt=Depends(get_rt)):
    return await rt.paper.trades(session_id, limit)


@api.get("/paper/equity")
async def paper_equity(session_id: int | None = None, rt=Depends(get_rt)):
    return await rt.paper.equity(session_id)


@api.get("/paper/sessions")
async def paper_sessions(rt=Depends(get_rt)):
    return await rt.paper.sessions()


# ============================================================================ portfolio
@api.get("/portfolio")
async def portfolio(rt=Depends(get_rt)):
    snap = rt.paper.snapshot()
    rebalancing = []
    if rt.paper.core is not None:
        pf = rt.paper.core.portfolio
        exchanges = sorted(pf.exchanges())
        for asset in sorted(pf.assets()):
            bals = {ex: pf.balance(ex, asset) for ex in exchanges}
            total = sum(bals.values(), Decimal(0))
            if total <= 0 or len(exchanges) < 2:
                continue
            shares = {ex: float(b / total * 100) for ex, b in bals.items()}
            if max(shares.values()) > 80:
                rebalancing.append({"asset": asset, "shares_pct": shares,
                                    "note": "inventory concentrated on one exchange; cross-exchange trades in the "
                                            "opposite direction will be constrained"})
    accounts = await rt.accounts.list()
    return {"mode": "paper", "simulated": True, "paper": snap, "rebalancing_needs": rebalancing,
            "live_accounts": accounts,
            "note": "Balances shown are VIRTUAL paper-trading balances. Live exchange balances are only fetched "
                    "read-only on request from the Settings page."}


# ============================================================================ reports
def _period_key(ts: datetime, period: str) -> str:
    if period == "weekly":
        y, w, _ = ts.isocalendar()
        return f"{y}-W{w:02d}"
    if period == "monthly":
        return ts.strftime("%Y-%m")
    return ts.date().isoformat()


async def _trade_rows(rt, source: str, run_id: int | None, session_id: int | None) -> list[dict]:
    if source == "backtest":
        if run_id is None:
            raise HTTPException(400, "run_id required for backtest reports")
        return await rt.backtests.get_trades(run_id, 100000)
    return await rt.paper.trades(session_id, 100000)


@api.get("/reports/summary")
async def report_summary(period: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
                         source: str = Query("paper", pattern="^(paper|backtest)$"), run_id: int | None = None,
                         session_id: int | None = None, rt=Depends(get_rt)):
    trades = await _trade_rows(rt, source, run_id, session_id)
    periods: dict[str, dict] = defaultdict(lambda: {"trades": 0, "successful": 0, "pnl_gbp": Decimal(0),
                                                    "fees_gbp": Decimal(0), "hedge_losses_gbp": Decimal(0),
                                                    "expected_net_gbp": Decimal(0), "notional_gbp": Decimal(0)})
    by_kind: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl_gbp": Decimal(0), "fees_gbp": Decimal(0)})
    by_ex: dict[str, dict] = defaultdict(lambda: {"trades": 0, "pnl_gbp": Decimal(0), "fees_gbp": Decimal(0)})
    for t in trades:
        ts = datetime.fromisoformat(t["ts"])
        p = periods[_period_key(ts, period)]
        pnl, fees = Decimal(t["pnl_gbp"]), Decimal(t["fees_gbp"])
        p["trades"] += 1
        p["successful"] += 1 if t["status"] == "success" else 0
        p["pnl_gbp"] += pnl
        p["fees_gbp"] += fees
        p["hedge_losses_gbp"] += Decimal(t["hedge_loss_gbp"])
        p["expected_net_gbp"] += Decimal(t["expected_net_gbp"] or 0)
        p["notional_gbp"] += Decimal(t["notional_gbp"] or 0)
        by_kind[t["kind"]]["trades"] += 1
        by_kind[t["kind"]]["pnl_gbp"] += pnl
        by_kind[t["kind"]]["fees_gbp"] += fees
        key = " / ".join(sorted(set(t["exchanges"])))
        by_ex[key]["trades"] += 1
        by_ex[key]["pnl_gbp"] += pnl
        by_ex[key]["fees_gbp"] += fees

    def ser(d):
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}
    return {"source": source, "period": period, "simulated": True,
            "periods": [{"period": k} | ser(v) for k, v in sorted(periods.items())],
            "by_strategy": {k: ser(v) for k, v in by_kind.items()},
            "by_exchange": {k: ser(v) for k, v in by_ex.items()},
            "note": "All figures are simulated (paper or backtest). P&L is net of fees and hedge losses."}


@api.get("/reports/csv")
async def report_csv(source: str = Query("paper", pattern="^(paper|backtest)$"), run_id: int | None = None,
                     session_id: int | None = None, rt=Depends(get_rt)):
    trades = await _trade_rows(rt, source, run_id, session_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "kind", "status", "route", "exchanges", "notional_gbp", "expected_net_gbp", "simulated_pnl_gbp",
                "fees_gbp", "hedge_loss_gbp"])
    for t in trades:
        w.writerow([t["ts"], t["kind"], t["status"], "->".join(t["route"]), "/".join(t["exchanges"]),
                    t["notional_gbp"], t["expected_net_gbp"], t["pnl_gbp"], t["fees_gbp"], t["hedge_loss_gbp"]])
    name = f"arbies_{source}_{run_id or session_id or 'current'}_trades.csv"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ============================================================================ strategies
class StrategyBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: str
    params: dict
    note: str = ""


class StrategyUpdate(BaseModel):
    params: dict
    note: str = ""


@api.get("/strategies")
async def strategies(rt=Depends(get_rt)):
    return await rt.strategies.list_current()


@api.post("/strategies")
async def create_strategy(body: StrategyBody, user: str = Depends(require_user), rt=Depends(get_rt)):
    try:
        res = await rt.strategies.create(body.name, body.kind, body.params, body.note, user)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await rt.audit(user, "strategy_create", body.name)
    return res


@api.put("/strategies/{name}")
async def update_strategy(name: str, body: StrategyUpdate, user: str = Depends(require_user), rt=Depends(get_rt)):
    try:
        res = await rt.strategies.update(name, body.params, body.note, user)
    except KeyError as e:
        raise HTTPException(404, "not found") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await rt.audit(user, "strategy_update", name, {"version": res["version"]})
    return res


@api.get("/strategies/{name}/versions")
async def strategy_versions(name: str, rt=Depends(get_rt)):
    return await rt.strategies.versions(name)


@api.post("/strategies/{name}/restore/{version}")
async def restore_strategy(name: str, version: int, user: str = Depends(require_user), rt=Depends(get_rt)):
    try:
        res = await rt.strategies.restore(name, version, user)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    await rt.audit(user, "strategy_restore", name, {"from_version": version, "new_version": res["version"]})
    return res


class EnableBody(BaseModel):
    enabled: bool


@api.post("/strategies/{name}/enabled")
async def enable_strategy(name: str, body: EnableBody, user: str = Depends(require_user), rt=Depends(get_rt)):
    try:
        res = await rt.strategies.set_enabled(name, body.enabled)
    except KeyError as e:
        raise HTTPException(404, "not found") from e
    await rt.audit(user, "strategy_enable_simulation", name, {"enabled": body.enabled})
    return res


# ============================================================================ agents
class AgentRunBody(BaseModel):
    input: dict = Field(default_factory=dict)
    use_llm: bool = True


@api.get("/agents")
async def agents(rt=Depends(get_rt)):
    return [{"name": s.name, "title": s.title, "description": s.description, "category": s.category,
             "tools": sorted(s.allowed_tools), "running": s.name in rt.agents.running,
             "schedule_minutes": s.default_schedule_minutes} for s in AGENTS.values()]


@api.post("/agents/{name}/run", dependencies=[Depends(rate_limit("agent_run", 20, 60))])
async def run_agent(name: str, body: AgentRunBody, user: str = Depends(require_user), rt=Depends(get_rt)):
    if name not in AGENTS:
        raise HTTPException(404, "unknown agent")
    await rt.audit(user, "agent_run", name, {"use_llm": body.use_llm})
    try:
        return await rt.agents.run(name, body.input, trigger=f"user:{user}", use_llm=body.use_llm)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@api.get("/agents/tasks")
async def agent_tasks(agent: str | None = None, limit: int = Query(50, le=500), rt=Depends(get_rt)):
    return await rt.agents.tasks(limit, agent)


@api.get("/agents/recommendations")
async def agent_recommendations(status: str | None = None, rt=Depends(get_rt)):
    return await rt.agents.recommendations(status)


class DecisionBody(BaseModel):
    decision: str = Field(pattern="^(approved|rejected|dismissed)$")


@api.post("/agents/recommendations/{rec_id}")
async def decide_recommendation(rec_id: int, body: DecisionBody, user: str = Depends(require_user), rt=Depends(get_rt)):
    try:
        res = await rt.agents.decide(rec_id, body.decision, user)
    except KeyError as e:
        raise HTTPException(404, "not found") from e
    await rt.audit(user, "recommendation_decision", str(rec_id), {"decision": body.decision})
    return res


@api.get("/agents/usage")
async def agent_usage(rt=Depends(get_rt)):
    return await rt.agents.usage()


# ============================================================================ risk
@api.get("/risk")
async def risk(rt=Depends(get_rt)):
    return rt.risk_status()


@api.get("/risk/events")
async def risk_events(limit: int = Query(100, le=1000), rt=Depends(get_rt)):
    async with rt.db.session() as s:
        rows = (await s.execute(select(RiskEventRecord).order_by(RiskEventRecord.id.desc()).limit(limit))).scalars().all()
    return [{"ts": r.ts.isoformat(), "mode": r.mode, "kind": r.kind, "severity": r.severity, "message": r.message,
             "data": r.data} for r in rows]


class KillBody(BaseModel):
    active: bool
    reason: str = ""
    password: str | None = None  # required to release


@api.post("/risk/kill-switch", dependencies=[Depends(rate_limit("kill", 10, 60))])
async def kill_switch(body: KillBody, request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    if not body.active and not (body.password and verify_password(body.password, rt.admin_password_hash)):
        raise HTTPException(403, "password required to release the kill switch")
    rt.set_kill_switch(body.active, body.reason or ("manual" if body.active else ""))
    await rt.audit(user, "kill_switch_on" if body.active else "kill_switch_off", detail={"reason": body.reason},
                   ip=_ip(request))
    return rt.risk_status()


# ============================================================================ settings
async def _save_override(rt, key: str, value: dict) -> None:
    async with rt.db.session() as s:
        row = await s.get(SettingKV, key)
        if row is None:
            s.add(SettingKV(key=key, value=value))
        else:
            row.value = value


@api.get("/settings")
async def get_settings_view(rt=Depends(get_rt)):
    return {"settings": rt.settings.public_dict(), "scanner_params": rt.scanner.params.model_dump(mode="json"),
            "scanner_enabled": rt.scanner.enabled,
            "paper_defaults": rt.paper.default_config().model_dump(mode="json"),
            "live_risk_limits": rt.live_risk.limits.model_dump(mode="json"),
            "retention_days": rt.recorder.retention_days if rt.recorder else rt.settings.data_retention_days,
            "credentials": await rt.accounts.list(), "live_gate": rt.gate.status()}


class ScannerBody(BaseModel):
    params: StrategyParams
    enabled: dict[str, bool] | None = None


@api.put("/settings/scanner")
async def put_scanner(body: ScannerBody, user: str = Depends(require_user), rt=Depends(get_rt)):
    rt.scanner.set_params(body.params)
    if body.enabled:
        rt.scanner.enabled.update({k: v for k, v in body.enabled.items() if k in rt.scanner.enabled})
    await _save_override(rt, "scanner", {"params": body.params.model_dump(mode="json"), "enabled": rt.scanner.enabled})
    await rt.audit(user, "settings_scanner", detail=body.params.model_dump(mode="json"))
    return {"params": rt.scanner.params.model_dump(mode="json"), "enabled": rt.scanner.enabled}


class PaperRiskBody(BaseModel):
    limits: RiskLimits


@api.put("/settings/paper-risk")
async def put_paper_risk(body: PaperRiskBody, user: str = Depends(require_user), rt=Depends(get_rt)):
    """Update risk limits of the running paper session (simulation only)."""
    if rt.paper.core is None:
        raise HTTPException(409, "no paper session; limits are set when starting a session")
    rt.paper.core.risk.update_limits(body.limits, approved_by_user=True, ts=utcnow())
    await rt.audit(user, "settings_paper_risk", detail=body.limits.model_dump(mode="json"))
    return rt.paper.core.risk.status(utcnow())


class LiveRiskBody(BaseModel):
    limits: RiskLimits
    password: str


@api.put("/settings/live-risk", dependencies=[Depends(rate_limit("live_risk", 5, 300))])
async def put_live_risk(body: LiveRiskBody, request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    if not verify_password(body.password, rt.admin_password_hash):
        raise HTTPException(403, "password re-authentication failed")
    rt.live_risk.update_limits(body.limits, approved_by_user=True, ts=utcnow())
    await rt.audit(user, "settings_live_risk", detail=body.limits.model_dump(mode="json"), ip=_ip(request))
    return rt.live_risk.status(utcnow())


class RetentionBody(BaseModel):
    days: int = Field(ge=1, le=3650)


@api.put("/settings/retention")
async def put_retention(body: RetentionBody, user: str = Depends(require_user), rt=Depends(get_rt)):
    if rt.recorder:
        rt.recorder.retention_days = body.days
    await _save_override(rt, "retention", {"days": body.days})
    await rt.audit(user, "settings_retention", detail={"days": body.days})
    return {"retention_days": body.days}


# ============================================================================ credentials / live
class CredentialBody(BaseModel):
    exchange: str
    api_key: str = Field(min_length=4, max_length=512)
    secret: str = Field(min_length=4, max_length=2048)
    password: str | None = None
    permissions: str = Field("read_only", pattern="^(read_only|trade)$")
    sandbox: bool = True
    admin_password: str


@api.post("/credentials", dependencies=[Depends(rate_limit("credentials", 5, 300))])
async def store_credentials(body: CredentialBody, request: Request, user: str = Depends(require_user),
                            rt=Depends(get_rt)):
    if not verify_password(body.admin_password, rt.admin_password_hash):
        raise HTTPException(403, "password re-authentication failed")
    if not rt.vault.configured:
        raise HTTPException(409, "ARBIES_SECRET_KEY is not configured on the backend; credentials cannot be stored")
    res = await rt.accounts.store(body.exchange, body.api_key, body.secret, body.password, body.permissions, body.sandbox)
    await rt.audit(user, "credentials_store", body.exchange, {"permissions": body.permissions, "sandbox": body.sandbox,
                                                              "fingerprint": res["key_fingerprint"]}, _ip(request))
    return res


@api.delete("/credentials/{exchange}")
async def delete_credentials(exchange: str, request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    ok = await rt.accounts.remove(exchange)
    await rt.audit(user, "credentials_delete", exchange, ip=_ip(request))
    return {"deleted": ok}


@api.get("/live/status")
async def live_status(rt=Depends(get_rt)):
    return {"gate": rt.gate.status(), "risk": rt.live_risk.status(utcnow()), "credentials": await rt.accounts.list(),
            "note": "Live trading is disabled unless ARBIES_LIVE_TRADING_ENABLED=true AND the arming flow is "
                    "completed. Automated live execution of strategies is not activated in this release."}


@api.get("/live/balances/{exchange}", dependencies=[Depends(rate_limit("live_bal", 10, 60))])
async def live_balances(exchange: str, user: str = Depends(require_user), rt=Depends(get_rt)):
    try:
        bal = await rt.accounts.fetch_balances(exchange)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"balance fetch failed: {type(e).__name__}: {e}") from e
    await rt.audit(user, "live_balance_read", exchange)
    return {"exchange": exchange, "read_only": True, "balances": {k: str(v) for k, v in bal.items()}}


class PreviewBody(BaseModel):
    exchanges: list[str]


@api.post("/live/preview")
async def live_preview(body: PreviewBody, user: str = Depends(require_user), rt=Depends(get_rt)):
    perms = await rt.accounts.permissions()
    preview = rt.gate.create_preview(body.exchanges, balances={}, limits=rt.live_risk.limits.model_dump(mode="json"),
                                     credentials={ex: perms.get(ex, "none") for ex in body.exchanges})
    await rt.audit(user, "live_preview", ",".join(body.exchanges))
    return preview


class ArmBody(BaseModel):
    preview_id: str
    confirmation_phrase: str
    password: str


@api.post("/live/arm", dependencies=[Depends(rate_limit("arm", 5, 600))])
async def live_arm(body: ArmBody, request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    if rt.global_kill:
        raise HTTPException(409, "kill switch active")
    ok = verify_password(body.password, rt.admin_password_hash)
    try:
        st = rt.gate.arm(body.preview_id, body.confirmation_phrase, ok, await rt.accounts.permissions())
    except LiveTradingDisabled as e:
        await rt.audit(user, "live_arm_refused", detail={"reason": str(e)}, ip=_ip(request))
        raise HTTPException(403, str(e)) from e
    await rt.audit(user, "live_armed", ",".join(st.exchanges), ip=_ip(request))
    return rt.gate.status()


@api.post("/live/disarm")
async def live_disarm(request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    rt.gate.disarm()
    await rt.audit(user, "live_disarmed", ip=_ip(request))
    return rt.gate.status()


# ============================================================================ historical investment simulator
class HistoricalImportBody(BaseModel):
    path: str  # file or directory, relative to ARBIES_HISTORICAL_DATA_DIR
    provider: str = "tardis.dev"
    source_url: str = ""
    sample_interval_ms: int = Field(1000, ge=100, le=60000)
    depth: int = Field(25, ge=1, le=100)
    notes: str = ""


def _historical_path(rt, rel: str):
    from pathlib import Path
    root = Path(rt.settings.historical_data_dir).resolve()
    p = (root / rel).resolve()
    if p != root and root not in p.parents:
        raise HTTPException(400, "path must be inside the configured historical data directory")
    if not p.exists():
        raise HTTPException(404, f"{rel} not found in {root}")
    return p


@api.get("/historical/coverage")
async def historical_coverage(start: datetime = Query(datetime(2025, 1, 1)), end: datetime | None = None,
                              rt=Depends(get_rt)):
    from datetime import UTC

    from arbies.historical.coverage import coverage_report
    start = start if start.tzinfo else start.replace(tzinfo=UTC)
    end = end or utcnow()
    end = end if end.tzinfo else end.replace(tzinfo=UTC)
    # minute-level coverage over tens of millions of rows takes seconds; cache it briefly per window
    key = (start.isoformat(), end.replace(second=0, microsecond=0).isoformat()[:15])
    cache = rt.historical.coverage_cache
    hit = cache.get(key)
    if hit and (utcnow() - hit[0]).total_seconds() < 600:
        return hit[1]
    rep = await coverage_report(rt.db, start, end)
    rep.pop("days", None)
    cache.clear()
    cache[key] = (utcnow(), rep)
    return rep


@api.get("/historical/datasets")
async def historical_datasets(rt=Depends(get_rt)):
    from arbies.historical.coverage import dataset_list
    return await dataset_list(rt.db)


@api.post("/historical/datasets/import")
async def historical_import(body: HistoricalImportBody, request: Request, user: str = Depends(require_user),
                            rt=Depends(get_rt)):
    from arbies.historical.importer import HistoricalImporter, ImportFormatError, iter_files
    p = _historical_path(rt, body.path)
    imp = HistoricalImporter(rt.db)
    out = []
    for f in iter_files([str(p)]):
        try:
            r = await imp.import_file(f, provider=body.provider, source_url=body.source_url,
                                      sample_interval_ms=body.sample_interval_ms, depth=body.depth, notes=body.notes)
            out.append({"file": f.name, "dataset_id": r.dataset_id, "status": r.status,
                        "snapshots_written": r.snapshots_written, "resumed_from_line": r.resumed_from_line,
                        "validation": r.validation})
        except ImportFormatError as e:
            out.append({"file": f.name, "status": "rejected", "error": str(e)})
    await rt.audit(user, "historical_import", body.path, {"files": len(out)}, _ip(request))
    return out


@api.get("/historical/reference")
async def historical_reference(rt=Depends(get_rt)):
    from arbies.historical.reference import reference_inventory
    return await reference_inventory(rt.db)


@api.get("/historical/fee-schedules")
async def historical_fee_schedules():
    from arbies.historical.plan import default_fee_schedules
    return {k: v.model_dump(mode="json") for k, v in default_fee_schedules().items()}


@api.get("/historical/defaults")
async def historical_defaults():
    from arbies.historical.simulator import PRESETS, STRATEGY_LABELS, SimulationConfig
    cfg = SimulationConfig()
    return {"config": cfg.model_dump(mode="json"), "presets": PRESETS, "strategies": STRATEGY_LABELS,
            "contributions": cfg.plan.totals(cfg.start, cfg.end)}


@api.post("/historical/simulations")
async def historical_submit(request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    from pydantic import ValidationError

    from arbies.historical.simulator import SimulationConfig
    try:
        cfg = SimulationConfig.model_validate(await request.json())
    except ValidationError as e:
        raise HTTPException(422, e.errors(include_url=False, include_context=False)) from e
    sid = await rt.historical.submit(cfg, requested_by=user)
    await rt.audit(user, "historical_simulation_submit", str(sid), ip=_ip(request))
    return {"id": sid, "status": "queued"}


@api.get("/historical/simulations")
async def historical_list(rt=Depends(get_rt)):
    return await rt.historical.list()


@api.get("/historical/simulations/{sid}")
async def historical_get(sid: int, rt=Depends(get_rt)):
    d = await rt.historical.get(sid)
    if d is None:
        raise HTTPException(404, "not found")
    return d


@api.get("/historical/simulations/{sid}/trades")
async def historical_trades(sid: int, variant: str | None = None, limit: int = Query(2000, le=20000),
                            rt=Depends(get_rt)):
    return await rt.historical.trades(sid, variant, limit)


@api.get("/historical/simulations/{sid}/csv")
async def historical_csv(sid: int, kind: str = "monthly", variant: str | None = None, rt=Depends(get_rt)):
    try:
        data = await rt.historical.csv(sid, kind, variant)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return StreamingResponse(iter([data]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="arbies-historical-{sid}-{kind}.csv"'})


@api.delete("/historical/simulations/{sid}")
async def historical_delete(sid: int, request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    ok = await rt.historical.delete(sid)
    await rt.audit(user, "historical_simulation_delete", str(sid), ip=_ip(request))
    return {"deleted": ok}


@api.post("/historical/simulations/{sid}/resume")
async def historical_resume(sid: int, request: Request, user: str = Depends(require_user), rt=Depends(get_rt)):
    from arbies.historical.simulator import SimulationConfig
    d = await rt.historical.get(sid)
    if d is None:
        raise HTTPException(404, "not found")
    if d["status"] in ("completed", "running", "queued"):
        raise HTTPException(409, f"simulation is {d['status']}")
    cfg = SimulationConfig.model_validate(d["config"])
    import asyncio
    t = asyncio.create_task(rt.historical.execute(sid, cfg))
    rt.historical._tasks.add(t)
    t.add_done_callback(rt.historical._tasks.discard)
    await rt.audit(user, "historical_simulation_resume", str(sid), ip=_ip(request))
    return {"id": sid, "status": "running"}
