"""Forward paper trading against live (or explicitly synthetic) market data.

Uses ``TradingCore`` - the same detection, risk and execution-simulation code
as the backtester. No exchange orders are ever placed from here.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, Field
from sqlalchemy import func, insert, select

from arbies.arbitrage.models import StrategyParams
from arbies.backtest.config import AllocationItem, BacktestConfig, RebalanceConfig
from arbies.config import Settings
from arbies.core.models import ZERO, utcnow
from arbies.core.valuation import Valuator
from arbies.db.models import PaperEquityPoint, PaperSession, PaperTrade
from arbies.db.session import Database
from arbies.execution.portfolio import Portfolio
from arbies.execution.simulator import ExecutionJob, FillModel
from arbies.logging_setup import get_logger
from arbies.market_data.hub import MarketDataHub
from arbies.risk.engine import RiskLimits
from arbies.services.trading_core import TradingCore

log = get_logger(__name__)


def _q(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.00000001"))


class PaperConfig(BaseModel):
    name: str = "Paper session"
    strategies: list[str] = Field(default_factory=lambda: ["cross_exchange", "triangular"])
    initial_capital_gbp: Decimal = Decimal("500")
    allocation: list[AllocationItem] | None = None
    params: StrategyParams = Field(default_factory=StrategyParams)
    fill: FillModel = Field(default_factory=FillModel)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    rebalance: RebalanceConfig = Field(default_factory=lambda: RebalanceConfig(enabled=False))
    decision_interval_ms: int = 500
    strategy_name: str | None = None
    strategy_version: int | None = None


class PaperTrader:
    def __init__(self, db: Database, hub: MarketDataHub, settings: Settings):
        self.db = db
        self.hub = hub
        self.settings = settings
        self.status = "stopped"  # stopped | running | paused
        self.session_id: int | None = None
        self.config: PaperConfig | None = None
        self.core: TradingCore | None = None
        self.initial_value = ZERO
        self._task: asyncio.Task | None = None
        self._trade_queue: list[dict] = []
        self.recent_jobs: list[dict] = []
        self.started_at: datetime | None = None
        self.last_error = ""

    def default_config(self) -> PaperConfig:
        s = self.settings
        return PaperConfig(
            initial_capital_gbp=s.paper_initial_capital_gbp,
            params=StrategyParams(min_net_profit_pct=s.min_net_profit_pct, max_trade_gbp=s.max_trade_size_gbp,
                                  max_data_age_ms=s.max_data_age_ms),
            risk=RiskLimits(max_position_gbp=s.max_trade_size_gbp, min_net_return_pct=s.min_net_profit_pct,
                            max_data_age_ms=s.max_data_age_ms),
        )

    def default_allocation(self, cfg: PaperConfig) -> list[AllocationItem]:
        exchanges = list(self.hub.adapters) or self.settings.exchanges
        symbols = sorted({sym for (_e, sym) in self.hub.books}) or self.settings.symbols
        tmp = BacktestConfig(exchanges=exchanges, symbols=symbols, start=utcnow(), end=utcnow() + timedelta(seconds=1),
                             allocation=cfg.allocation)
        return tmp.resolved_allocation()

    # ------------------------------------------------------------------ balances for the scanner
    def balance_fn(self):
        if self.core is None:
            return None
        pf = self.core.portfolio
        return lambda ex, asset: pf.available(ex, asset)

    # ------------------------------------------------------------------ lifecycle
    def _build_core(self, cfg: PaperConfig, portfolio: Portfolio) -> TradingCore:
        return TradingCore(strategies=cfg.strategies, params=cfg.params, rules=self.hub.rules, fill=cfg.fill,
                           risk_limits=cfg.risk, portfolio=portfolio, reporting=self.settings.reporting_currency,
                           fallback_fx=self.settings.fallback_fx_to_gbp, mode="paper", rebalance=cfg.rebalance,
                           on_job_done=self._on_job_done, keep_jobs=False)

    async def start(self, cfg: PaperConfig | None = None) -> dict:
        if self.status == "running":
            return self.snapshot()
        if self.status == "paused" and self.core is not None:
            self.status = "running"
            await self._persist_session(status="running")
            self._ensure_task()
            return self.snapshot()
        cfg = cfg or self.default_config()
        books = self.hub.fresh_books()
        if not books:
            raise RuntimeError("no fresh market data available; cannot value the virtual portfolio")
        val = Valuator(self.settings.reporting_currency, self.settings.fallback_fx_to_gbp)
        val.update_from_books(books.values())
        now = utcnow()
        pf = Portfolio(keep_ledger=False)
        alloc = self.default_allocation(cfg) if cfg.allocation is None else cfg.allocation
        cfg = cfg.model_copy(update={"allocation": alloc})
        for item in alloc:
            amt = val.from_reporting(item.asset, cfg.initial_capital_gbp * item.fraction)
            if amt is None:
                continue
            pf.deposit(now, item.exchange, item.asset, amt.quantize(Decimal("0.00000001")))
        self.core = self._build_core(cfg, pf)
        self.core.update_valuation(books)
        self.initial_value, _ = pf.total_value(self.core.valuator)
        self.config = cfg
        async with self.db.session() as s:
            sess = PaperSession(name=cfg.name, status="running", started_at=now, config=cfg.model_dump(mode="json"),
                                initial_value_gbp=self.initial_value, balances=pf.snapshot(),
                                data_source=self.hub.data_source())
            s.add(sess)
            await s.flush()
            self.session_id = sess.id
        self.status = "running"
        self.started_at = now
        self.recent_jobs = []
        self._ensure_task()
        return self.snapshot()

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="paper-trader")

    async def pause(self) -> dict:
        if self.status == "running":
            self.status = "paused"
            await self._persist_session(status="paused")
        return self.snapshot()

    async def stop(self) -> dict:
        if self.status in ("running", "paused"):
            self.status = "stopped"
            if self._task:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
            await self._flush_trades()
            await self._persist_session(status="stopped", stopped_at=utcnow())
        return self.snapshot()

    async def reset(self) -> dict:
        await self.stop()
        self.core = None
        self.session_id = None
        self.config = None
        self.initial_value = ZERO
        self.recent_jobs = []
        return self.snapshot()

    async def restore(self) -> None:
        """Restore the most recent unfinished session as *paused* after a restart."""
        async with self.db.session() as s:
            sess = (await s.execute(select(PaperSession).where(PaperSession.status.in_(["running", "paused"]))
                                    .order_by(PaperSession.id.desc()).limit(1))).scalar_one_or_none()
        if sess is None:
            return
        if sess.data_source != self.hub.configured_source():
            # never continue a session on a different data provenance (e.g. synthetic -> live)
            async with self.db.session() as s:
                row = await s.get(PaperSession, sess.id)
                row.status = "stopped"
                row.stopped_at = utcnow()
            log.warning("not restoring paper session recorded on different data source",
                        session_id=sess.id, session_source=sess.data_source, current=self.hub.configured_source())
            return
        cfg = PaperConfig(**sess.config)
        pf = Portfolio(keep_ledger=False)
        for row in sess.balances or []:
            pf.balances[(row["exchange"], row["asset"])] = Decimal(row["amount"])
        self.core = self._build_core(cfg, pf)
        self.config = cfg
        self.session_id = sess.id
        self.initial_value = sess.initial_value_gbp
        self.started_at = sess.started_at
        self.status = "paused"
        await self._persist_session(status="paused")
        log.info("restored paper session as paused", session_id=sess.id)

    # ------------------------------------------------------------------ loop
    def _on_job_done(self, job: ExecutionJob) -> None:
        d = job.to_dict()
        self.recent_jobs.insert(0, d)
        del self.recent_jobs[200:]
        if self.session_id is not None:
            self._trade_queue.append({
                "session_id": self.session_id, "ts": job.created, "kind": job.opp.kind.value,
                "status": job.status.value, "route": job.opp.route, "exchanges": job.opp.exchanges,
                "expected_net_gbp": job.opp.net_profit_gbp, "pnl_gbp": job.pnl_gbp, "fees_gbp": job.fees_gbp,
                "hedge_loss_gbp": job.hedge_loss_gbp, "notional_gbp": job.opp.notional_gbp, "detail": d})

    async def _loop(self) -> None:
        tick = 0.1
        decision_iv = timedelta(milliseconds=self.config.decision_interval_ms if self.config else 500)
        last_decision = utcnow() - decision_iv
        last_persist = utcnow()
        while self.status in ("running", "paused"):
            try:
                now = utcnow()
                core = self.core
                if core is None:
                    break
                core.process_jobs(lambda e, s: self.hub.books.get((e, s)), now)
                if self.status == "running" and now - last_decision >= decision_iv:
                    last_decision = now
                    core.decide(self.hub.fresh_books(now), now, self.hub.healthy_map(now))
                    core.maybe_rebalance(now, list(self.hub.adapters))
                if now - last_persist >= timedelta(seconds=10):
                    last_persist = now
                    await self._flush_trades()
                    await self._persist_equity(now)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.exception("paper loop error")
            await asyncio.sleep(tick)

    async def _flush_trades(self) -> None:
        rows, self._trade_queue = self._trade_queue, []
        if rows:
            async with self.db.session() as s:
                await s.execute(insert(PaperTrade), rows)

    async def _persist_equity(self, now: datetime) -> None:
        if self.core is None or self.session_id is None:
            return
        self.core.update_valuation(self.hub.fresh_books(now))
        value, est = self.core.portfolio.total_value(self.core.valuator)
        async with self.db.session() as s:
            realised = (await s.execute(select(func.coalesce(func.sum(PaperTrade.pnl_gbp), 0))
                                        .where(PaperTrade.session_id == self.session_id))).scalar_one()
            s.add(PaperEquityPoint(session_id=self.session_id, ts=now, value_gbp=value,
                                   realised_pnl_gbp=Decimal(str(realised)), estimated=est))
            sess = await s.get(PaperSession, self.session_id)
            sess.balances = self.core.portfolio.snapshot()

    async def _persist_session(self, **fields) -> None:
        if self.session_id is None:
            return
        async with self.db.session() as s:
            sess = await s.get(PaperSession, self.session_id)
            if sess is None:
                return
            for k, v in fields.items():
                setattr(sess, k, v)
            if self.core is not None:
                sess.balances = self.core.portfolio.snapshot()

    # ------------------------------------------------------------------ views
    def snapshot(self) -> dict:
        now = utcnow()
        out = {
            "status": self.status, "session_id": self.session_id, "simulated": True,
            "data_source": self.hub.data_source(), "started_at": self.started_at.isoformat() if self.started_at else None,
            "initial_value_gbp": str(_q(self.initial_value)), "last_error": self.last_error,
            "config": self.config.model_dump(mode="json") if self.config else self.default_config().model_dump(mode="json"),
        }
        if self.core is None:
            return out
        core = self.core
        core.update_valuation(self.hub.fresh_books(now))
        value, est = core.portfolio.total_value(core.valuator)
        realised = sum((Decimal(j["pnl_gbp"]) for j in self.recent_jobs), ZERO)
        exp = core.risk.exposures(core.portfolio, core.valuator)
        out.update({
            "value_gbp": str(_q(value)), "valuation_estimated": est,
            "total_pnl_gbp": str(_q(value - self.initial_value)),
            "simulated_realised_pnl_recent_gbp": str(realised),
            "daily_realised_pnl_gbp": str(core.risk.daily_pnl(now)),
            "balances": core.portfolio.snapshot(core.valuator),
            "value_by_exchange": {k: str(_q(v)) for k, v in core.portfolio.value_by_exchange(core.valuator).items()},
            "value_by_asset": {k: str(_q(v)) for k, v in core.portfolio.value_by_asset(core.valuator).items()},
            "exposure": {"total_gbp": str(_q(exp["total"])), "by_exchange": {k: str(_q(v)) for k, v in exp["by_exchange"].items()},
                         "by_asset": {k: str(_q(v)) for k, v in exp["by_asset"].items()}},
            "active_jobs": [j.to_dict() for j in core.active_jobs],
            "counters": dict(core.counters), "risk_rejections": dict(core.risk_rejections),
            "rejection_counts": dict(core.rejection_counts), "risk": core.risk.status(now),
            "in_transit": [{"arrival": a.isoformat(), "exchange": e, "asset": x, "amount": str(m)}
                           for a, e, x, m in core.portfolio.in_transit],
        })
        return out

    async def trades(self, session_id: int | None = None, limit: int = 500) -> list[dict]:
        sid = session_id or self.session_id
        if sid is None:
            return []
        async with self.db.session() as s:
            rows = (await s.execute(select(PaperTrade).where(PaperTrade.session_id == sid)
                                    .order_by(PaperTrade.ts.desc()).limit(limit))).scalars().all()
        return [{"id": t.id, "ts": t.ts.isoformat(), "kind": t.kind, "status": t.status, "route": t.route,
                 "exchanges": t.exchanges, "expected_net_gbp": str(t.expected_net_gbp) if t.expected_net_gbp is not None else None,
                 "pnl_gbp": str(t.pnl_gbp), "fees_gbp": str(t.fees_gbp), "hedge_loss_gbp": str(t.hedge_loss_gbp),
                 "notional_gbp": str(t.notional_gbp) if t.notional_gbp is not None else None, "detail": t.detail}
                for t in rows]

    async def equity(self, session_id: int | None = None) -> list[dict]:
        sid = session_id or self.session_id
        if sid is None:
            return []
        async with self.db.session() as s:
            rows = (await s.execute(select(PaperEquityPoint).where(PaperEquityPoint.session_id == sid)
                                    .order_by(PaperEquityPoint.ts))).scalars().all()
        return [{"ts": r.ts.isoformat(), "value_gbp": str(r.value_gbp), "realised_pnl_gbp": str(r.realised_pnl_gbp),
                 "estimated": r.estimated} for r in rows]

    async def sessions(self) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(PaperSession).order_by(PaperSession.id.desc()).limit(50))).scalars().all()
        return [{"id": r.id, "name": r.name, "status": r.status, "created_at": r.created_at.isoformat(),
                 "started_at": r.started_at.isoformat() if r.started_at else None,
                 "stopped_at": r.stopped_at.isoformat() if r.stopped_at else None,
                 "initial_value_gbp": str(r.initial_value_gbp), "data_source": r.data_source} for r in rows]
