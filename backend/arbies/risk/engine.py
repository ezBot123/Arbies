"""Deterministic risk engine.

Every order flow (backtest, paper, live) must pass ``RiskEngine.check`` before
execution. The engine is plain deterministic code: AI agents have no handle
on it beyond read-only status, and nothing here calls an LLM.

Defaults are deliberately restrictive.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel

from arbies.arbitrage.models import Opportunity
from arbies.core.models import ZERO, Side, split_symbol
from arbies.core.valuation import Valuator
from arbies.execution.portfolio import Portfolio


class RiskLimits(BaseModel):
    max_position_gbp: Decimal = Decimal("25")  # per opportunity notional
    max_total_exposure_gbp: Decimal = Decimal("500")  # all non-reporting-currency inventory
    max_exchange_exposure_gbp: Decimal = Decimal("300")
    max_asset_exposure_gbp: Decimal = Decimal("300")
    min_net_return_pct: Decimal = Decimal("0.30")
    max_slippage_pct: Decimal = Decimal("0.50")  # estimated depth slippage as % of notional
    max_data_age_ms: int = 5000
    max_daily_loss_gbp: Decimal = Decimal("25")
    max_open_order_exposure_gbp: Decimal = Decimal("50")
    max_inventory_imbalance_pct: Decimal = Decimal("90")  # share of an asset on a single exchange
    min_confidence: float = 0.2
    require_exchange_healthy: bool = True
    circuit_breaker_failures: int = 3  # consecutive failed executions
    circuit_breaker_cooldown_s: int = 300
    duplicate_window_ms: int = 2000  # same opportunity key cannot be re-submitted inside this window
    max_trades_per_minute: int = 30
    # Fiat and fiat-pegged assets that do not count as market exposure.
    cash_assets: list[str] = ["GBP", "USD", "EUR", "USDC", "USDT"]


class RiskViolation(StrEnum):
    KILL_SWITCH = "kill_switch"
    CIRCUIT_BREAKER = "circuit_breaker"
    NOT_QUALIFIED = "opportunity_not_qualified"
    POSITION_SIZE = "max_position_size"
    TOTAL_EXPOSURE = "max_total_exposure"
    EXCHANGE_EXPOSURE = "max_exchange_exposure"
    ASSET_EXPOSURE = "max_asset_exposure"
    MIN_PROFIT = "min_net_profit"
    SLIPPAGE = "max_slippage"
    STALE_DATA = "stale_data"
    DAILY_LOSS = "max_daily_loss"
    OPEN_ORDERS = "max_open_order_exposure"
    INVENTORY_IMBALANCE = "inventory_imbalance"
    EXCHANGE_UNHEALTHY = "exchange_unhealthy"
    DUPLICATE = "duplicate_order"
    LOW_CONFIDENCE = "low_confidence"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    RATE_LIMIT = "trade_rate_limit"
    NO_VALUATION = "no_valuation"
    INVALID_ORDER = "invalid_order"


@dataclass(slots=True)
class RiskDecision:
    approved: bool
    violations: list[RiskViolation] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"approved": self.approved, "violations": [v.value for v in self.violations], "details": self.details}


@dataclass(slots=True)
class RiskEvent:
    ts: datetime
    kind: str
    severity: str  # info | warning | critical
    message: str
    data: dict = field(default_factory=dict)


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None, mode: str = "paper"):
        self.limits = limits or RiskLimits()
        self.mode = mode
        self.kill_switch = False
        self.kill_reason = ""
        self._consecutive_failures = 0
        self._breaker_until: datetime | None = None
        self._recent_keys: dict[str, datetime] = {}
        self._submitted_client_ids: set[str] = set()
        self._trade_times: deque[datetime] = deque()
        self._daily_pnl: dict[str, Decimal] = {}  # YYYY-MM-DD -> realised pnl (GBP)
        self.open_order_exposure_gbp = ZERO
        self.events: list[RiskEvent] = []
        self.listeners: list = []  # callables(RiskEvent)

    # ------------------------------------------------------------------ events
    def _emit(self, ts: datetime, kind: str, severity: str, message: str, **data) -> None:
        ev = RiskEvent(ts, kind, severity, message, data)
        self.events.append(ev)
        if len(self.events) > 2000:
            self.events = self.events[-1000:]
        for fn in self.listeners:
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - listeners must never break risk checks
                pass

    # ------------------------------------------------------------------ controls
    def activate_kill_switch(self, ts: datetime, reason: str) -> None:
        self.kill_switch = True
        self.kill_reason = reason
        self._emit(ts, "kill_switch", "critical", f"Kill switch activated: {reason}")

    def release_kill_switch(self, ts: datetime) -> None:
        self.kill_switch = False
        self.kill_reason = ""
        self._emit(ts, "kill_switch_released", "warning", "Kill switch released")

    def breaker_active(self, now: datetime) -> bool:
        return self._breaker_until is not None and now < self._breaker_until

    def record_execution(self, now: datetime, success: bool, realised_pnl_gbp: Decimal = ZERO) -> None:
        day = now.date().isoformat()
        self._daily_pnl[day] = self._daily_pnl.get(day, ZERO) + realised_pnl_gbp
        if success:
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.limits.circuit_breaker_failures:
                self._breaker_until = now + timedelta(seconds=self.limits.circuit_breaker_cooldown_s)
                self._emit(now, "circuit_breaker", "critical",
                           f"Circuit breaker tripped after {self._consecutive_failures} consecutive failures",
                           until=self._breaker_until.isoformat())
                self._consecutive_failures = 0
        if self._daily_pnl[day] <= -self.limits.max_daily_loss_gbp:
            self._emit(now, "daily_loss_limit", "critical", "Maximum daily loss reached; trading halted for the day",
                       daily_pnl=str(self._daily_pnl[day]))

    def daily_pnl(self, now: datetime) -> Decimal:
        return self._daily_pnl.get(now.date().isoformat(), ZERO)

    def register_client_order_id(self, client_order_id: str) -> bool:
        """Duplicate-order protection. Returns False if the id was already used."""
        if client_order_id in self._submitted_client_ids:
            return False
        self._submitted_client_ids.add(client_order_id)
        return True

    def update_limits(self, new_limits: RiskLimits, approved_by_user: bool, ts: datetime) -> None:
        """Change limits. Live-mode changes require explicit user approval."""
        if self.mode == "live" and not approved_by_user:
            raise PermissionError("live risk limits can only be changed with explicit user approval")
        self.limits = new_limits
        self._emit(ts, "limits_updated", "warning", "Risk limits updated", mode=self.mode)

    # ------------------------------------------------------------------ exposure
    def exposures(self, portfolio: Portfolio, valuator: Valuator) -> dict:
        """Market exposure = value of non-cash (crypto) inventory, in the reporting currency."""
        by_ex: dict[str, Decimal] = {}
        by_asset: dict[str, Decimal] = {}
        per_asset_ex: dict[str, dict[str, Decimal]] = {}
        total = ZERO
        for (ex, asset), amt in portfolio.balances.items():
            if asset in self.limits.cash_assets or amt <= 0:
                continue
            v = valuator.to_reporting(asset, amt)
            by_ex[ex] = by_ex.get(ex, ZERO) + v
            by_asset[asset] = by_asset.get(asset, ZERO) + v
            per_asset_ex.setdefault(asset, {})[ex] = v
            total += v
        return {"total": total, "by_exchange": by_ex, "by_asset": by_asset, "per_asset_exchange": per_asset_ex}

    # ------------------------------------------------------------------ main check
    def check(self, opp: Opportunity, portfolio: Portfolio, valuator: Valuator, now: datetime,
              exchange_healthy: dict[str, bool] | None = None) -> RiskDecision:
        L = self.limits
        v: list[RiskViolation] = []
        d: dict[str, str] = {}

        if self.kill_switch:
            v.append(RiskViolation.KILL_SWITCH)
            d["kill_switch"] = self.kill_reason
        if self.breaker_active(now):
            v.append(RiskViolation.CIRCUIT_BREAKER)
            d["circuit_breaker_until"] = self._breaker_until.isoformat() if self._breaker_until else ""
        if not opp.qualified:
            v.append(RiskViolation.NOT_QUALIFIED)

        notional_gbp = opp.notional_gbp
        if notional_gbp is None:
            v.append(RiskViolation.NO_VALUATION)
            notional_gbp = ZERO
        if notional_gbp > L.max_position_gbp:
            v.append(RiskViolation.POSITION_SIZE)
            d["notional_gbp"] = str(notional_gbp)
        if opp.net_return_pct < L.min_net_return_pct:
            v.append(RiskViolation.MIN_PROFIT)
        if opp.notional > 0 and opp.depth_slippage / opp.notional * 100 > L.max_slippage_pct:
            v.append(RiskViolation.SLIPPAGE)
        if opp.data_age_ms > L.max_data_age_ms:
            v.append(RiskViolation.STALE_DATA)
        if opp.confidence < L.min_confidence:
            v.append(RiskViolation.LOW_CONFIDENCE)
        if self.daily_pnl(now) <= -L.max_daily_loss_gbp:
            v.append(RiskViolation.DAILY_LOSS)
        if self.open_order_exposure_gbp + notional_gbp > L.max_open_order_exposure_gbp:
            v.append(RiskViolation.OPEN_ORDERS)
        if L.require_exchange_healthy and exchange_healthy is not None:
            bad = [ex for ex in opp.exchanges if not exchange_healthy.get(ex, False)]
            if bad:
                v.append(RiskViolation.EXCHANGE_UNHEALTHY)
                d["unhealthy"] = ",".join(bad)

        last = self._recent_keys.get(opp.key)
        if last is not None and (now - last).total_seconds() * 1000 < L.duplicate_window_ms:
            v.append(RiskViolation.DUPLICATE)

        while self._trade_times and (now - self._trade_times[0]).total_seconds() > 60:
            self._trade_times.popleft()
        if len(self._trade_times) >= L.max_trades_per_minute:
            v.append(RiskViolation.RATE_LIMIT)

        # balances & projected exposure after the trade
        projected = portfolio.copy()
        projected.keep_ledger = False
        try:
            if opp.kind.value == "cross_exchange":
                # cross-exchange fees are charged in the quote currency of each leg
                for leg in opp.legs:
                    projected.apply_trade(now, leg.exchange, leg.symbol, leg.side, leg.base_amount,
                                          leg.quote_amount, leg.fee, split_symbol(leg.symbol)[1])
            else:
                # triangular legs are sequential and funded by the previous leg; only the
                # first leg needs pre-existing inventory. Fees are in the received currency.
                for leg in opp.legs:
                    projected.apply_trade(now, leg.exchange, leg.symbol, leg.side, leg.base_amount,
                                          leg.quote_amount, leg.fee, leg.fee_currency)
        except Exception:  # InsufficientBalance
            v.append(RiskViolation.INSUFFICIENT_BALANCE)
        # exposure limits block trades that would leave exposure above a limit AND increase it
        exp = self.exposures(projected, valuator)
        cur_exp = self.exposures(portfolio, valuator)
        if exp["total"] > L.max_total_exposure_gbp and exp["total"] > cur_exp["total"]:
            v.append(RiskViolation.TOTAL_EXPOSURE)
        if any(x > L.max_exchange_exposure_gbp and x > cur_exp["by_exchange"].get(k, ZERO)
               for k, x in exp["by_exchange"].items()):
            v.append(RiskViolation.EXCHANGE_EXPOSURE)
        if any(x > L.max_asset_exposure_gbp and x > cur_exp["by_asset"].get(k, ZERO)
               for k, x in exp["by_asset"].items()):
            v.append(RiskViolation.ASSET_EXPOSURE)
        if opp.kind.value == "cross_exchange" and opp.legs:
            base = split_symbol(opp.legs[0].symbol)[0]
            per_ex = exp["per_asset_exchange"].get(base, {})
            tot = sum(per_ex.values(), ZERO)
            if tot > 0 and len(portfolio.exchanges()) > 1:
                share = max(per_ex.values()) / tot * 100
                cur = cur_exp["per_asset_exchange"].get(base, {})
                cur_tot = sum(cur.values(), ZERO)
                cur_share = max(cur.values()) / cur_tot * 100 if cur_tot > 0 else Decimal(100)
                # only block trades that make an existing imbalance worse
                if share > L.max_inventory_imbalance_pct and share > cur_share:
                    v.append(RiskViolation.INVENTORY_IMBALANCE)

        approved = not v
        if approved:
            self._recent_keys[opp.key] = now
            self._trade_times.append(now)
        return RiskDecision(approved, v, d)

    def status(self, now: datetime) -> dict:
        return {
            "mode": self.mode,
            "kill_switch": self.kill_switch,
            "kill_reason": self.kill_reason,
            "circuit_breaker_active": self.breaker_active(now),
            "circuit_breaker_until": self._breaker_until.isoformat() if self.breaker_active(now) else None,
            "consecutive_failures": self._consecutive_failures,
            "daily_pnl_gbp": str(self.daily_pnl(now)),
            "open_order_exposure_gbp": str(self.open_order_exposure_gbp),
            "limits": self.limits.model_dump(mode="json"),
        }
