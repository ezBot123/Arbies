"""The six Arbies agents and the runner that executes them.

Every agent first performs a deterministic analysis using real system data via
its permitted tools. If an LLM provider is configured (and within budget), the
LLM then reviews that analysis - optionally calling further permitted tools -
and writes a narrative report. Without a provider the deterministic report is
returned on its own; the agent is still useful, just less verbose.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import func, select

from arbies.agents.providers import LLMProvider
from arbies.agents.tools import ToolGateway, registry
from arbies.core.models import utcnow
from arbies.db.models import AgentRecommendation, AgentTask
from arbies.logging_setup import get_logger

log = get_logger(__name__)


class RecommendationDraft(BaseModel):
    title: str
    detail: str
    severity: str = "info"
    proposed_change: dict | None = None


class AgentReport(BaseModel):
    summary: str
    findings: list[str] = Field(default_factory=list)
    recommendations: list[RecommendationDraft] = Field(default_factory=list)
    metrics: dict = Field(default_factory=dict)
    narrative: str | None = None  # LLM-written text, if a provider ran


READ_TOOLS = {n for n, t in registry.tools.items() if t.permission.value == "read"}

AnalyzeFn = Callable[[ToolGateway, dict], Awaitable[AgentReport]]


@dataclass
class AgentSpec:
    name: str
    title: str
    description: str
    category: str
    allowed_tools: set[str]
    analyze: AnalyzeFn
    system_prompt: str = ""
    default_schedule_minutes: int | None = None
    extra: dict = field(default_factory=dict)


COMMON_RULES = (
    "You are an analysis agent inside Arbies, a cryptocurrency arbitrage research platform. "
    "Rules: never claim or imply guaranteed profit; clearly distinguish estimated opportunity profit, "
    "simulated (paper/backtest) results and real results; state data provenance (live/synthetic) when "
    "relevant; report losses and failures plainly; you cannot execute trades, enable live trading, change "
    "risk limits or move funds - only analyse and propose. Use tools to check facts before stating them. "
    "Finish with a concise report: a short summary paragraph, then bullet findings, then numbered "
    "recommendations (each actionable and testable)."
)


def _f(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


# ============================================================================ analyses
async def analyze_health(gw: ToolGateway, inp: dict) -> AgentReport:
    health = await gw.call("get_exchange_health")
    errs = await gw.call("get_recent_errors", {"limit": 20})
    status = await gw.call("get_system_status")
    findings, recs = [], []
    unhealthy = [h for h in health if not h["healthy"]]
    for h in health:
        line = (f"{h['exchange']}: {h['status']} via {h['transport']}, {h['updates']} updates, "
                f"{h['errors']} errors, {h['reconnects']} reconnects")
        if h["stale_symbols"]:
            line += f", stale: {', '.join(h['stale_symbols'])}"
        if h["unavailable_symbols"]:
            line += f", unavailable: {', '.join(h['unavailable_symbols'])}"
        findings.append(line)
        if h["latency_ms_ewma"] is not None and h["latency_ms_ewma"] > 2000:
            recs.append(RecommendationDraft(title=f"High feed latency on {h['exchange']}",
                                            detail=f"Smoothed exchange->receive latency is {h['latency_ms_ewma']} ms "
                                                   "(includes clock offset). Check network/clock sync.",
                                            severity="warning"))
    for h in unhealthy:
        recs.append(RecommendationDraft(
            title=f"{h['exchange']} market data unhealthy",
            detail=f"Status {h['status']}; last error: {h['last_error'] or 'n/a'}. Opportunities involving this "
                   "exchange are being rejected. Check connectivity, exchange status page and API restrictions.",
            severity="critical" if h["status"] == "error" else "warning"))
    rec = errs.get("recorder") or {}
    if rec.get("write_errors"):
        recs.append(RecommendationDraft(title="Market data recorder write errors",
                                        detail=f"{rec['write_errors']} failed flushes. Check database health.",
                                        severity="warning"))
    scan = errs.get("scanner") or {}
    if _f(scan.get("avg_scan_ms")) > 200:
        recs.append(RecommendationDraft(title="Slow opportunity scans",
                                        detail=f"Average scan time {scan['avg_scan_ms']} ms; consider fewer symbols "
                                               "or triangular start currencies.", severity="info"))
    if status.get("market_data_mode") == "synthetic":
        findings.append("Market data mode is SYNTHETIC - nothing observed reflects real markets.")
    summary = (f"{len(health) - len(unhealthy)}/{len(health)} exchange feeds healthy; "
               f"{len(errs.get('connection_events', []))} recent connection events.")
    return AgentReport(summary=summary, findings=findings, recommendations=recs,
                       metrics={"healthy": len(health) - len(unhealthy), "total": len(health),
                                "avg_scan_ms": scan.get("avg_scan_ms"), "recorder_rows": rec.get("rows_written")})


async def analyze_research(gw: ToolGateway, inp: dict) -> AgentReport:
    hours = int(inp.get("hours", 24))
    stats = await gw.call("get_opportunity_statistics", {"hours": hours})
    liq = await gw.call("get_market_liquidity")
    avail = await gw.call("get_data_availability")
    findings, recs = [], []
    routes = stats["routes"]
    qualified = [r for r in routes if r["qualified_records"] > 0]
    findings.append(f"{stats['records']} opportunity records in the last {hours}h across {len(routes)} routes; "
                    f"{len(qualified)} routes qualified at least once (estimated, pre-execution).")
    for r in qualified[:5]:
        findings.append(f"{r['key']}: {r['qualified_records']} qualified records, max est. net "
                        f"{r['max_net_return_pct']:.3f}% ({r['data_source']})")
    if stats["qualified_by_hour_utc"]:
        peak = max(stats["qualified_by_hour_utc"].items(), key=lambda kv: kv[1])
        findings.append(f"Qualified opportunities peak at {peak[0]:02d}:00 UTC ({peak[1]} records).")
    thin = [m for m in liq if m["spread_pct"] > 0.5 or (m["bid_depth_0_5pct"] * (m["mid"] or 0)) < 1000]
    for m in thin[:5]:
        findings.append(f"Thin/wide market: {m['exchange']} {m['symbol']} spread {m['spread_pct']:.3f}%")
    for r in qualified[:3]:
        parts = r["key"].split(":", 1)[1].split("|")
        recs.append(RecommendationDraft(
            title=f"Backtest route {r['key']}",
            detail=("This route qualified repeatedly in live scanning. Validate with a walk-forward backtest on "
                    "recorded data before drawing conclusions; estimates exclude queue position and real latency."),
            proposed_change={"action": "run_backtest", "route": parts}))
    real = [a for a in avail if a["source"] == "live_recorded"]
    if not real:
        recs.append(RecommendationDraft(title="No real recorded market data yet",
                                        detail="Enable live market data with recording so research can use real "
                                               "observations rather than synthetic data.", severity="warning"))
    if not qualified:
        summary = f"No routes met the profit threshold in the last {hours}h of scanning."
    else:
        summary = f"{len(qualified)} routes met the threshold at least once in the last {hours}h; top candidates listed."
    return AgentReport(summary=summary, findings=findings, recommendations=recs,
                       metrics={"routes": len(routes), "qualified_routes": len(qualified),
                                "records": stats["records"], "markets_observed": len(liq)})


async def analyze_strategy(gw: ToolGateway, inp: dict) -> AgentReport:
    runs = await gw.call("list_backtests", {"limit": 10})
    completed = [r for r in runs if r["status"] == "completed"]
    findings, recs, metrics = [], [], {}
    for r in completed[:5]:
        h = r["headline"]
        findings.append(f"Run #{r['id']} ({'+'.join(r['strategies'] or [])}, {r['data_source']}): P&L "
                        f"£{_f(h.get('total_pnl_gbp')):.2f}, {h.get('trades_attempted')} trades, max DD "
                        f"{_f(h.get('max_drawdown_pct')):.2f}%")
    losing = [r for r in completed if _f(r["headline"].get("total_pnl_gbp")) < 0]
    if losing:
        recs.append(RecommendationDraft(title="Investigate losing backtests",
                                        detail=f"{len(losing)} of {len(completed)} recent runs lost money. Review "
                                               "failed hedges and fee share in their metrics.", severity="info"))
    if inp.get("run_walkforward") and inp.get("exchanges") and inp.get("symbols"):
        wf = await gw.call("run_walkforward_optimisation", {
            "exchanges": inp["exchanges"], "symbols": inp["symbols"], "hours_back": int(inp.get("hours_back", 72)),
            "data_source": inp.get("data_source", "live_recorded"),
            "strategies": inp.get("strategies", ["cross_exchange"])})
        metrics["walkforward_id"] = wf["id"]
        findings.append(f"Walk-forward study #{wf['id']} locked parameters {wf['selected_params']} on validation "
                        f"data; out-of-sample test is sealed until the user runs it.")
        recs.append(RecommendationDraft(title=f"Run sealed out-of-sample test for study #{wf['id']}",
                                        detail="Selection is locked. Run the out-of-sample test once to obtain an "
                                               "unbiased estimate.", proposed_change={"walkforward_id": wf["id"]}))
    if not completed:
        recs.append(RecommendationDraft(title="No completed backtests",
                                        detail="Record market data, then run baseline backtests for each strategy."))
    summary = f"Reviewed {len(completed)} completed backtests; {len(losing)} were unprofitable."
    metrics.update({"completed_runs": len(completed), "losing_runs": len(losing)})
    return AgentReport(summary=summary, findings=findings, recommendations=recs, metrics=metrics)


async def analyze_risk(gw: ToolGateway, inp: dict) -> AgentReport:
    risk = await gw.call("get_risk_status")
    paper = await gw.call("get_paper_trading_status")
    events = await gw.call("get_risk_events", {"limit": 50})
    trades = await gw.call("get_paper_trades", {"limit": 50})
    liq = await gw.call("get_market_liquidity")
    findings, recs = [], []
    if risk.get("kill_switch"):
        findings.append(f"Kill switch ACTIVE: {risk.get('kill_reason')}")
    crit = [e for e in events if e["severity"] == "critical"]
    findings.append(f"{len(events)} recent risk events ({len(crit)} critical).")
    failed = [t for t in trades if t["status"] != "success"]
    if trades:
        rate = len(failed) / len(trades) * 100
        findings.append(f"{len(failed)}/{len(trades)} recent paper executions were not clean successes ({rate:.0f}%).")
        if rate > 30:
            recs.append(RecommendationDraft(title="High execution failure rate",
                                            detail="More than 30% of simulated executions failed or needed hedging. "
                                                   "Consider a higher profit threshold or longer latency assumption.",
                                            severity="warning",
                                            proposed_change={"min_net_profit_pct": "increase"}))
        hedge = sum(_f(t["hedge_loss_gbp"]) for t in trades)
        findings.append(f"Hedge losses across recent paper trades: £{hedge:.2f}")
    exp = paper.get("exposure") or {}
    total = _f(exp.get("total_gbp"))
    value = _f(paper.get("value_gbp"))
    if value > 0:
        by_asset = {k: _f(v) for k, v in (exp.get("by_asset") or {}).items()}
        for a, v in by_asset.items():
            share = v / value * 100
            if share > 50:
                recs.append(RecommendationDraft(title=f"Concentration in {a}",
                                                detail=f"{a} is {share:.0f}% of the paper portfolio value.",
                                                severity="warning"))
        findings.append(f"Crypto exposure £{total:.2f} of £{value:.2f} paper portfolio value.")
    wide = [m for m in liq if m["spread_pct"] > 1.0]
    for m in wide[:3]:
        recs.append(RecommendationDraft(title=f"Poor liquidity: {m['exchange']} {m['symbol']}",
                                        detail=f"Spread {m['spread_pct']:.2f}%. Exclude from strategies until it improves.",
                                        severity="info"))
    if risk.get("circuit_breaker_active"):
        findings.append("Circuit breaker currently active.")
    summary = f"{len(crit)} critical risk events; exposure £{total:.2f}; " + (
        "kill switch active." if risk.get("kill_switch") else "kill switch off.")
    return AgentReport(summary=summary, findings=findings, recommendations=recs,
                       metrics={"critical_events": len(crit), "exposure_gbp": total,
                                "failed_executions": len(failed), "executions": len(trades)})


async def analyze_performance(gw: ToolGateway, inp: dict) -> AgentReport:
    paper = await gw.call("get_paper_trading_status")
    trades = await gw.call("get_paper_trades", {"limit": 50})
    runs = await gw.call("list_backtests", {"limit": 5})
    since = utcnow() - timedelta(hours=24)
    recent = [t for t in trades if t["ts"] >= since.isoformat()]
    pnl = sum(_f(t["pnl_gbp"]) for t in recent)
    fees = sum(_f(t["fees_gbp"]) for t in recent)
    hedge = sum(_f(t["hedge_loss_gbp"]) for t in recent)
    est = sum(_f(t["expected_net_gbp"]) for t in recent)
    findings = [
        f"Paper session status: {paper.get('status')} (SIMULATED; data source {paper.get('data_source')}).",
        f"Last 24h simulated trades: {len(recent)}, simulated P&L £{pnl:.4f}, fees £{fees:.4f}, hedge losses £{hedge:.4f}.",
        f"Estimated net before execution for these trades: £{est:.4f}; realised-vs-estimate gap £{pnl - est:.4f}.",
    ]
    if paper.get("total_pnl_gbp") is not None:
        findings.append(f"Paper portfolio total change since start: £{_f(paper.get('total_pnl_gbp')):.4f} "
                        "(includes inventory price moves).")
    for r in [r for r in runs if r["status"] == "completed"][:3]:
        findings.append(f"Backtest #{r['id']}: P&L £{_f(r['headline'].get('total_pnl_gbp')):.2f}, fees "
                        f"£{_f(r['headline'].get('fees_paid_gbp')):.2f}")
    recs = []
    if recent and fees > abs(pnl) and pnl <= 0:
        recs.append(RecommendationDraft(title="Fees dominate results",
                                        detail="Trading fees exceed simulated P&L. Lower-fee venues or fee tiers, "
                                               "or a higher threshold, may be needed."))
    summary = f"{len(recent)} simulated trades in 24h with simulated P&L £{pnl:.4f} (fees £{fees:.4f})."
    return AgentReport(summary=summary, findings=findings, recommendations=recs,
                       metrics={"trades_24h": len(recent), "sim_pnl_24h_gbp": pnl, "fees_24h_gbp": fees,
                                "hedge_losses_24h_gbp": hedge})


async def analyze_historical(gw: ToolGateway, inp: dict) -> AgentReport:
    sim = await gw.call("get_historical_simulation", {"id": inp.get("simulation_id")})
    if "error" in sim:
        return AgentReport(summary="No completed historical simulation to analyse.",
                           recommendations=[RecommendationDraft(
                               title="Run the Historical Investment Simulator",
                               detail="Fetch daily reference prices and run the default £500 + £100/month scenario.")])
    h = sim["headline"]
    findings, recs = [], []
    findings.append(f"Status: {h['status']}. Contributions £{_f(h['total_personal_contributions_gbp']):.2f}; ending "
                    f"value £{_f(h['ending_value_gbp']):.2f} ({h['ending_value_meaning']})")
    findings.append(f"Trading P&L £{_f(h['trading_pnl_gbp']):.2f} ({h['trading_pnl_scope']}); inventory/market P&L "
                    f"£{_f(h['market_inventory_pnl_gbp']):.2f}; conversion costs £{_f(h['conversion_costs_gbp']):.2f}.")
    cov = sim["coverage"]
    findings.append(f"Order-book coverage with >= 2 exchanges: {cov['overlap_minutes']} minutes "
                    f"({cov['overlap_pct']}% of the period).")
    by_set = {}
    for v in sim["variants"].values():
        s = v["summary"]
        if v["preset"] == "moderate" and v["fee_schedule"] == "base":
            by_set[v["label"]] = _f(s["trading"]["net_trading_profit_gbp"])
        findings.append(f"{v['label']} [{v['preset']}, fees {v['fee_schedule']}]: {s['trading']['trades']} trades, "
                        f"net trading £{_f(s['trading']['net_trading_profit_gbp']):.4f}, eligible opportunities "
                        f"{s['trading']['opportunities_eligible']}.")
    if by_set:
        best = max(by_set.items(), key=lambda kv: kv[1])
        if all(v == best[1] for v in by_set.values()):
            findings.append(f"No strategy set outperformed another: all made £{best[1]:.4f} net trading P&L in the "
                            "observed periods.")
        else:
            findings.append(f"Best strategy set after costs (observed periods only): {best[0]} (£{best[1]:.4f}).")
    b = sim["benchmarks"]
    findings.append(f"Benchmarks: cash £{_f(b['cash']['final_value_gbp']):.2f}; BTC buy-and-hold "
                    f"£{_f(b['btc']['final_value_gbp']):.2f} (full BTC price risk).")
    for p in sim["screening"]["pairs"]:
        share = p["share_of_minutes_above_fees_pct"]
        findings.append(f"Candle screening {p['pair']}: {p['minutes_both_traded']} minutes compared; close gap above "
                        f"round-trip fees in {share} % of minutes (indicator only; not executable).")
    unverified = [e for sch in (sim.get("fees") or {}).values() for e in sch["entries"]
                  if e["verification"] in ("unverified", "secondary_sources")]
    if unverified:
        findings.append(f"{len(unverified)} fee-schedule entries are not confirmed by an official source.")
        recs.append(RecommendationDraft(title="Confirm account fee tiers",
                                        detail="Several historical fee entries rest on secondary sources. Confirm "
                                               "them from account statements and set custom fees.", severity="warning"))
    if not cov["continuous"]:
        cov_data = await gw.call("get_historical_coverage")
        have_imports = any(d["status"] == "completed" for d in cov_data["datasets"])
        recs.append(RecommendationDraft(
            title="Continuous historical order-book data is needed for a full-period answer",
            detail=(f"Trading was observable in {cov['overlap_pct']}% of the period. "
                    + ("Imported free samples are already used; a full answer needs a continuous paid dataset "
                       "(only after explicit approval of the cost), or targeted days around large market moves. "
                       if have_imports else
                       "Import genuine Level 2 data (e.g. Tardis.dev first-of-month samples) and re-run. ")
                    + "Do not extrapolate the observed sample."), severity="warning",
            proposed_change={"action": "import_historical_data"}))
    traded = any(v["summary"]["trading"]["trades"] for v in sim["variants"].values())
    recs.append(RecommendationDraft(
        title="Re-run the parameter study once more data exists",
        detail=("Select presets on the training window only and confirm on the unseen test window. "
                + ("No variant traded in either window, so the current split cannot distinguish presets."
                   if not traded else "The current sample is small; treat the selection as provisional."))))
    return AgentReport(summary=f"Historical simulation {sim['id']}: {h['status']}.", findings=findings,
                       recommendations=recs,
                       metrics={"simulation_id": sim["id"], "overlap_pct": cov["overlap_pct"],
                                "answerable": h["answerable"]})


AGENTS: dict[str, AgentSpec] = {}


def _spec(name, title, description, category, tools, analyze, schedule=None):
    AGENTS[name] = AgentSpec(name, title, description, category, set(tools), analyze,
                             system_prompt=f"{COMMON_RULES}\nYour role: {title}. {description}",
                             default_schedule_minutes=schedule)


_spec("system_health", "System Health Agent", "Checks exchange connectivity, data freshness, error logs and "
      "processing delays, and recommends maintenance.", "health",
      ["get_exchange_health", "get_recent_errors", "get_system_status", "get_data_availability",
       "propose_recommendation"], analyze_health, 15)
_spec("market_research", "Market Research Agent", "Analyses historical opportunities, liquidity and exchange "
      "behaviour to recommend markets for further testing.", "research",
      ["get_opportunity_statistics", "get_market_liquidity", "get_data_availability", "get_current_opportunities",
       "get_exchange_health", "propose_recommendation"], analyze_research, 360)
_spec("strategy_optimisation", "Strategy Optimisation Agent", "Runs authorised backtests and walk-forward "
      "optimisations, compares strategies and investigates poor performance using objective metrics.", "strategy",
      ["list_backtests", "get_backtest", "get_walkforward_study", "run_backtest", "run_walkforward_optimisation",
       "get_data_availability", "list_strategies", "propose_recommendation"], analyze_strategy)
_spec("risk_analysis", "Risk Analysis Agent", "Analyses exposure, failed executions, drawdowns, liquidity and "
      "concentration risk and produces risk recommendations. Hard limits are enforced by the deterministic risk "
      "engine, not by you.", "risk",
      ["get_risk_status", "get_risk_events", "get_paper_trading_status", "get_paper_trades", "get_market_liquidity",
       "list_backtests", "propose_recommendation"], analyze_risk, 60)
_spec("performance_analyst", "Performance Analyst", "Produces readable performance summaries, explains costs "
      "and compares simulated strategies.", "performance",
      ["get_paper_trading_status", "get_paper_trades", "list_backtests", "get_backtest", "propose_recommendation"],
      analyze_performance, 1440)


_spec("historical_research", "Historical Research Agent", "Interprets historical investment simulations: "
      "coverage, strategy comparison, fee sensitivity, benchmarks and weaknesses of the backtest, and proposes "
      "follow-up experiments. It can read results but cannot run, change or delete simulations.", "research",
      ["get_historical_simulation", "get_historical_coverage", "propose_recommendation"], analyze_historical)


async def analyze_orchestrator(gw: ToolGateway, inp: dict) -> AgentReport:  # replaced at runtime by runner
    raise NotImplementedError


AGENTS["orchestrator"] = AgentSpec(
    "orchestrator", "Orchestrator", "Coordinates the other agents, reviews their results, summarises system "
    "performance, recommends priorities and escalates errors.", "orchestration",
    {"get_system_status", "get_risk_status", "get_exchange_health", "propose_recommendation"},
    analyze_orchestrator, system_prompt=f"{COMMON_RULES}\nYour role: Orchestrator. Review the sub-agent reports "
    "given to you, prioritise issues and summarise the state of the system.", default_schedule_minutes=60)


# ============================================================================ runner
class AgentRunner:
    def __init__(self, runtime, provider: LLMProvider | None, max_tool_calls: int = 12,
                 monthly_budget_usd: Decimal = Decimal("20")):
        self.rt = runtime
        self.provider = provider
        self.max_tool_calls = max_tool_calls
        self.monthly_budget = monthly_budget_usd
        self.running: set[str] = set()

    async def month_spend(self) -> Decimal:
        start = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        async with self.rt.db.session() as s:
            v = (await s.execute(select(func.coalesce(func.sum(AgentTask.est_cost_usd), 0))
                                 .where(AgentTask.created_at >= start))).scalar_one()
        return Decimal(str(v))

    async def store_recommendation(self, agent: str, task_id: int | None, title: str, detail: str, severity: str,
                                   proposed_change: dict | None) -> int:
        spec = AGENTS.get(agent)
        async with self.rt.db.session() as s:
            r = AgentRecommendation(task_id=task_id, agent=agent, title=title[:200], detail=detail,
                                    category=spec.category if spec else "general", severity=severity,
                                    proposed_change=proposed_change, status="open")
            s.add(r)
            await s.flush()
            return r.id

    async def run(self, name: str, inp: dict | None = None, trigger: str = "manual", use_llm: bool = True) -> dict:
        if name not in AGENTS:
            raise KeyError(name)
        if name in self.running:
            raise RuntimeError(f"agent {name} is already running")
        inp = inp or {}
        spec = AGENTS[name]
        self.running.add(name)
        async with self.rt.db.session() as s:
            task = AgentTask(agent=name, status="running", trigger=trigger, input=inp, output={}, tool_calls=[])
            s.add(task)
            await s.flush()
            task_id = task.id
        gw = ToolGateway(registry, self.rt, name, spec.allowed_tools)
        out: dict = {}
        llm_meta: dict = {"llm_used": False}
        try:
            if name == "orchestrator":
                report = await self._orchestrate(gw, inp)
            else:
                report = await spec.analyze(gw, inp)
            if use_llm and self.provider is not None:
                if await self.month_spend() >= self.monthly_budget:
                    report.findings.append("LLM narrative skipped: monthly AI budget reached.")
                else:
                    llm_meta = await self._narrate(spec, gw, report)
                    report.narrative = llm_meta.get("text")
            elif self.provider is None:
                report.findings.append("No AI provider configured: deterministic analysis only.")
            for rec in report.recommendations:
                await self.store_recommendation(name, task_id, rec.title, rec.detail, rec.severity, rec.proposed_change)
            out = report.model_dump(mode="json")
            status, error = "completed", ""
        except Exception as e:  # noqa: BLE001
            log.exception("agent failed", agent=name)
            status, error = "failed", str(e)[:2000]
        finally:
            self.running.discard(name)
        async with self.rt.db.session() as s:
            task = await s.get(AgentTask, task_id)
            task.status = status
            task.error = error
            task.output = out
            task.finished_at = utcnow()
            task.tool_calls = [c.__dict__ for c in gw.calls]
            task.llm_used = llm_meta.get("llm_used", False)
            task.model = llm_meta.get("model")
            task.input_tokens = llm_meta.get("input_tokens", 0)
            task.output_tokens = llm_meta.get("output_tokens", 0)
            task.est_cost_usd = llm_meta.get("cost", Decimal(0))
        return {"task_id": task_id, "status": status, "error": error, "report": out}

    async def _narrate(self, spec: AgentSpec, gw: ToolGateway, report: AgentReport) -> dict:
        prompt = ("Here is the deterministic analysis produced from live system data:\n\n"
                  + json.dumps(report.model_dump(mode="json"), indent=1, default=str)
                  + "\n\nVerify anything important with your tools, then write your report for the operator.")
        res = await self.provider.run(spec.system_prompt, prompt, gw, self.max_tool_calls)
        return {"llm_used": True, "text": res.text + ("\n\n" + "; ".join(res.notes) if res.notes else ""),
                "model": res.model, "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                "cost": res.est_cost_usd}

    async def _orchestrate(self, gw: ToolGateway, inp: dict) -> AgentReport:
        sub = inp.get("agents") or ["system_health", "risk_analysis", "market_research", "performance_analyst"]
        findings, recs, metrics = [], [], {}
        escalations = 0
        for name in sub:
            if name == "orchestrator" or name not in AGENTS:
                continue
            res = await self.run(name, inp.get(name, {}), trigger="orchestrator", use_llm=False)
            rep = res.get("report") or {}
            metrics[name] = {"task_id": res["task_id"], "status": res["status"]}
            findings.append(f"[{AGENTS[name].title}] {rep.get('summary', res.get('error'))}")
            for r in rep.get("recommendations", []):
                if r.get("severity") == "critical":
                    escalations += 1
                    findings.append(f"ESCALATION from {AGENTS[name].title}: {r['title']}")
            if res["status"] != "completed":
                escalations += 1
                findings.append(f"ESCALATION: {name} failed: {res['error']}")
        status = await gw.call("get_system_status")
        if escalations:
            recs.append(RecommendationDraft(title=f"{escalations} item(s) need attention",
                                            detail="See escalations in the orchestrator report.", severity="warning"))
        summary = (f"Ran {len(metrics)} agents; {escalations} escalation(s). Mode: {status.get('operating_mode')}, "
                   f"market data: {status.get('market_data_mode')}.")
        metrics["escalations"] = escalations
        return AgentReport(summary=summary, findings=findings, recommendations=recs, metrics=metrics)

    async def tasks(self, limit: int = 50, agent: str | None = None) -> list[dict]:
        async with self.rt.db.session() as s:
            q = select(AgentTask).order_by(AgentTask.id.desc()).limit(limit)
            if agent:
                q = q.where(AgentTask.agent == agent)
            rows = (await s.execute(q)).scalars().all()
        return [{"id": t.id, "agent": t.agent, "status": t.status, "trigger": t.trigger,
                 "created_at": t.created_at.isoformat(), "finished_at": t.finished_at.isoformat() if t.finished_at else None,
                 "input": t.input, "output": t.output, "tool_calls": t.tool_calls, "llm_used": t.llm_used,
                 "model": t.model, "input_tokens": t.input_tokens, "output_tokens": t.output_tokens,
                 "est_cost_usd": str(t.est_cost_usd), "error": t.error} for t in rows]

    async def recommendations(self, status: str | None = None, limit: int = 100) -> list[dict]:
        async with self.rt.db.session() as s:
            q = select(AgentRecommendation).order_by(AgentRecommendation.id.desc()).limit(limit)
            if status:
                q = q.where(AgentRecommendation.status == status)
            rows = (await s.execute(q)).scalars().all()
        return [{"id": r.id, "agent": r.agent, "task_id": r.task_id, "created_at": r.created_at.isoformat(),
                 "title": r.title, "detail": r.detail, "category": r.category, "severity": r.severity,
                 "proposed_change": r.proposed_change, "status": r.status,
                 "decided_at": r.decided_at.isoformat() if r.decided_at else None, "decided_by": r.decided_by}
                for r in rows]

    async def decide(self, rec_id: int, decision: str, user: str) -> dict:
        """Human decision on a recommendation. Approval only records intent; no change is
        auto-applied to live trading. Simulation changes are applied by the user explicitly."""
        if decision not in ("approved", "rejected", "dismissed"):
            raise ValueError("invalid decision")
        async with self.rt.db.session() as s:
            r = await s.get(AgentRecommendation, rec_id)
            if r is None:
                raise KeyError(rec_id)
            r.status = decision
            r.decided_at = utcnow()
            r.decided_by = user
        return {"id": rec_id, "status": decision}

    async def usage(self) -> dict:
        async with self.rt.db.session() as s:
            rows = (await s.execute(select(AgentTask.agent, func.count(), func.sum(AgentTask.input_tokens),
                                           func.sum(AgentTask.output_tokens), func.sum(AgentTask.est_cost_usd))
                                    .group_by(AgentTask.agent))).all()
        return {"month_spend_usd": str(await self.month_spend()), "monthly_budget_usd": str(self.monthly_budget),
                "by_agent": {a: {"tasks": n, "input_tokens": int(i or 0), "output_tokens": int(o or 0),
                                 "est_cost_usd": str(c or 0)} for a, n, i, o, c in rows}}
