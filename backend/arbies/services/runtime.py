"""Application runtime: wires all components together and manages their lifecycle."""

from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy import insert, select

from arbies.agents.agents import AGENTS, AgentRunner
from arbies.agents.providers import build_provider
from arbies.backtest.service import BacktestService, WalkForwardService
from arbies.config import MarketDataMode, Settings
from arbies.core.models import utcnow
from arbies.db.models import AuditLog, Exchange, Market, RiskEventRecord, SettingKV, SystemLog
from arbies.db.session import Database
from arbies.execution.live import LiveTradingGate
from arbies.historical.service import HistoricalService
from arbies.logging_setup import get_logger
from arbies.market_data.hub import UK_ELIGIBILITY_NOTES, MarketDataHub
from arbies.market_data.recorder import MarketDataRecorder
from arbies.paper.service import PaperTrader
from arbies.risk.engine import RiskEngine, RiskEvent, RiskLimits
from arbies.security import CredentialVault, RateLimiter, hash_password
from arbies.services.live_accounts import LiveAccounts
from arbies.services.scanner import Scanner
from arbies.services.strategies import StrategyStore

log = get_logger(__name__)


class Runtime:
    def __init__(self, settings: Settings, db: Database | None = None, hub: MarketDataHub | None = None):
        self.settings = settings
        self.db = db or Database(settings.database_url, settings.db_echo)
        self.hub = hub or MarketDataHub(settings)
        self.started_at: datetime | None = None
        self.recorder: MarketDataRecorder | None = None
        self.paper = PaperTrader(self.db, self.hub, settings)
        self.scanner = Scanner(self.hub, self.db, settings, balance_provider=self.paper.balance_fn)
        self.backtests = BacktestService(self.db, lambda: dict(self.hub.rules), settings.fallback_fx_to_gbp)
        self.walkforward = WalkForwardService(self.db, self.backtests)
        self.historical = HistoricalService(self.db, self.backtests.rules_for, self.backtests.code_revision,
                                            workers=max(1, min(3, (os.cpu_count() or 2) - 1)))
        self.strategies = StrategyStore(self.db)
        self.vault = CredentialVault(settings.secret_key.get_secret_value() if settings.secret_key else None)
        self.accounts = LiveAccounts(self.db, self.vault)
        self.gate = LiveTradingGate(settings.live_trading_enabled, settings.live_confirmation_phrase)
        # live-mode risk engine (separate from paper sessions; same rules)
        self.live_risk = RiskEngine(RiskLimits(max_position_gbp=settings.max_trade_size_gbp,
                                               min_net_return_pct=settings.min_net_profit_pct), mode="live")
        self.live_risk.listeners.append(self._on_risk_event)
        self.agents = AgentRunner(self, build_provider(settings), settings.ai_max_tool_calls,
                                  settings.ai_monthly_budget_usd)
        self.rate_limiter = RateLimiter()
        self.admin_password_hash: str = ""
        self.generated_password: str | None = None
        self.global_kill = False
        self.global_kill_reason = ""
        self._risk_events: list[dict] = []
        self._logs: list[dict] = []
        self._tasks: list[asyncio.Task] = []
        self.ws_clients: set = set()
        self.paper.risk_listener = self._on_risk_event
        self._wrap_paper_core_builder()

    # ------------------------------------------------------------------ wiring helpers
    def _wrap_paper_core_builder(self) -> None:
        original = self.paper._build_core

        def build(cfg, pf):
            core = original(cfg, pf)
            core.risk.listeners.append(self._on_risk_event)
            if self.global_kill:
                core.risk.activate_kill_switch(utcnow(), self.global_kill_reason)
            return core
        self.paper._build_core = build

    def _on_risk_event(self, ev: RiskEvent) -> None:
        mode = "live" if ev in self.live_risk.events else "paper"
        self._risk_events.append({"ts": ev.ts, "mode": mode, "kind": ev.kind, "severity": ev.severity,
                                  "message": ev.message, "data": ev.data})

    def log_event(self, level: str, component: str, message: str, **data) -> None:
        self._logs.append({"ts": utcnow(), "level": level, "component": component, "message": message, "data": data})

    async def audit(self, actor: str, action: str, target: str = "", detail: dict | None = None,
                    ip: str | None = None) -> None:
        async with self.db.session() as s:
            s.add(AuditLog(actor=actor, action=action, target=target, detail=detail or {}, ip=ip))

    # ------------------------------------------------------------------ lifecycle
    async def start(self, start_market_data: bool = True) -> None:
        self.started_at = utcnow()
        await self._init_auth()
        await self.strategies.seed_defaults(self.settings.exchanges, self.settings.symbols)
        await self._seed_exchanges()
        await self._load_overrides()
        await self.paper.restore()
        if start_market_data and self.settings.market_data_mode != MarketDataMode.OFF:
            await self.hub.start()
            if self.settings.record_market_data:
                self.recorder = MarketDataRecorder(self.hub, self.db, self.settings.record_interval_ms,
                                                   self.settings.record_depth, self.settings.data_retention_days)
                await self.recorder.start()
            await self.scanner.start()
        self._tasks.append(asyncio.create_task(self._persist_loop(), name="runtime-persist"))
        self._tasks.append(asyncio.create_task(self._agent_scheduler(), name="agent-scheduler"))
        self.log_event("info", "runtime", "Arbies started", market_data_mode=self.settings.market_data_mode.value)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.paper.suspend()  # kept as paused, restored on next start
        await self.scanner.stop()
        if self.recorder:
            await self.recorder.stop()
        await self.hub.stop()
        await self._flush()
        await self.db.dispose()

    async def _init_auth(self) -> None:
        s = self.settings
        if s.admin_password is not None:
            self.admin_password_hash = hash_password(s.admin_password.get_secret_value())
            return
        async with self.db.session() as sess:
            row = await sess.get(SettingKV, "admin_password_hash")
            if row is not None:
                self.admin_password_hash = row.value["hash"]
                return
            pw = secrets.token_urlsafe(12)
            self.admin_password_hash = hash_password(pw)
            sess.add(SettingKV(key="admin_password_hash", value={"hash": self.admin_password_hash}))
        self.generated_password = pw
        log.warning("No ARBIES_ADMIN_PASSWORD set - generated an admin password (shown once)",
                    username=s.admin_username, password=pw)

    async def _load_overrides(self) -> None:
        from arbies.arbitrage.models import StrategyParams
        async with self.db.session() as s:
            scanner = await s.get(SettingKV, "scanner")
            retention = await s.get(SettingKV, "retention")
        if scanner is not None:
            try:
                self.scanner.set_params(StrategyParams(**scanner.value["params"]))
                self.scanner.enabled.update(scanner.value.get("enabled", {}))
            except Exception:  # noqa: BLE001
                log.warning("ignoring invalid stored scanner settings")
        if retention is not None:
            self.settings.data_retention_days = int(retention.value["days"])

    async def _seed_exchanges(self) -> None:
        async with self.db.session() as s:
            for ex in self.settings.exchanges:
                if await s.get(Exchange, ex) is None:
                    s.add(Exchange(id=ex, name=ex.capitalize(), enabled=True,
                                   uk_eligibility_note=UK_ELIGIBILITY_NOTES.get(ex, "Verify UK eligibility.")))

    async def _persist_loop(self) -> None:
        last_rules = utcnow() - timedelta(minutes=10)
        while True:
            await asyncio.sleep(2)
            try:
                await self._flush()
                if utcnow() - last_rules > timedelta(minutes=5) and self.hub.rules:
                    await self._persist_rules()
                    last_rules = utcnow()
            except Exception:  # noqa: BLE001
                log.exception("persist loop error")

    async def _flush(self) -> None:
        ev, self._risk_events = self._risk_events, []
        lg, self._logs = self._logs, []
        if not ev and not lg:
            return
        async with self.db.session() as s:
            if ev:
                await s.execute(insert(RiskEventRecord), ev)
            if lg:
                await s.execute(insert(SystemLog), lg)

    async def _persist_rules(self) -> None:
        async with self.db.session() as s:
            existing = {(m.exchange, m.symbol): m for m in (await s.execute(select(Market))).scalars().all()}
            for (ex, sym), r in self.hub.rules.items():
                m = existing.get((ex, sym))
                if m is None:
                    m = Market(exchange=ex, symbol=sym, base=r.base, quote=r.quote)
                    s.add(m)
                m.taker_fee, m.maker_fee, m.fee_source = r.taker_fee, r.maker_fee, r.fee_source
                m.amount_step, m.price_step, m.min_amount, m.min_cost = r.amount_step, r.price_step, r.min_amount, r.min_cost
                m.active = r.active

    async def _agent_scheduler(self) -> None:
        last: dict[str, datetime] = {}
        while True:
            await asyncio.sleep(60)
            if not self.settings.agents_schedule_enabled:
                continue
            now = utcnow()
            for name, spec in AGENTS.items():
                if not spec.default_schedule_minutes or name in self.agents.running:
                    continue
                if name in last and now - last[name] < timedelta(minutes=spec.default_schedule_minutes):
                    continue
                last[name] = now
                try:
                    await self.agents.run(name, trigger="schedule", use_llm=self.settings.agents_schedule_use_llm)
                except Exception:  # noqa: BLE001
                    log.exception("scheduled agent failed", agent=name)

    # ------------------------------------------------------------------ controls
    def set_kill_switch(self, active: bool, reason: str = "") -> None:
        now = utcnow()
        self.global_kill = active
        self.global_kill_reason = reason
        engines = [self.live_risk] + ([self.paper.core.risk] if self.paper.core else [])
        for eng in engines:
            if active:
                eng.activate_kill_switch(now, reason or "manual")
            else:
                eng.release_kill_switch(now)
        if active:
            self.gate.disarm()

    # ------------------------------------------------------------------ status views
    def system_status(self) -> dict:
        now = utcnow()
        health = self.hub.health()
        return {
            "app": "Arbies",
            "version": "0.1.0",
            "env": self.settings.env,
            "operating_mode": "live" if self.gate.is_armed() else "paper",
            "live_trading_env_enabled": self.settings.live_trading_enabled,
            "live_armed": self.gate.is_armed(),
            "market_data_mode": self.settings.market_data_mode.value,
            "data_source": self.hub.data_source(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "uptime_s": (now - self.started_at).total_seconds() if self.started_at else 0,
            "exchanges": {ex: h.to_dict(now, self.settings.max_data_age_ms) for ex, h in health.items()},
            "books": len(self.hub.books),
            "scanner": self.scanner.stats,
            "recorder": self.recorder.status() if self.recorder else None,
            "paper_status": self.paper.status,
            "kill_switch": self.global_kill,
            "kill_reason": self.global_kill_reason,
            "ai_provider": self.settings.ai_provider if self.settings.ai_configured else "none",
            "code_revision": self.backtests.code_revision,
        }

    def risk_status(self) -> dict:
        now = utcnow()
        paper = self.paper.core.risk.status(now) if self.paper.core else None
        return {"kill_switch": self.global_kill, "kill_reason": self.global_kill_reason,
                "circuit_breaker_active": bool(paper and paper["circuit_breaker_active"]),
                "paper": paper, "live": self.live_risk.status(now), "live_gate": self.gate.status()}
