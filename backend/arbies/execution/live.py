"""Live trading infrastructure - DISABLED BY DEFAULT.

Layers of protection (all must pass before any real order is sent):

1. ``ARBIES_LIVE_TRADING_ENABLED=true`` in the backend environment.
2. An explicit arming flow through the API: the user re-enters their password,
   reviews a preview (exchanges, balances, limits, risks) and types the exact
   confirmation phrase. Arming lives in memory only and expires; a restart
   always returns to disarmed.
3. Credentials for the exchange stored with ``permissions="trade"``. Credentials
   are added as ``read_only`` by default.
4. The deterministic risk engine (kill switch, circuit breaker, limits) and
   order pre-validation against exchange rules.
5. Duplicate-order protection via unique client order ids.

There is no code path for withdrawals or transfers: the exchange wrapper
refuses those method names outright. AI agents have no tool that reaches this
module.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from arbies.core.models import MarketRules, Side, utcnow
from arbies.risk.engine import RiskEngine

FORBIDDEN_METHODS = {
    "withdraw", "transfer", "fetchDepositAddress", "fetch_deposit_address", "createDepositAddress",
    "create_deposit_address", "transferIn", "transferOut", "transfer_in", "transfer_out",
}
READ_ONLY_METHODS = {
    "fetch_balance", "fetch_open_orders", "fetch_closed_orders", "fetch_my_trades", "fetch_order",
    "fetch_trading_fees", "load_markets", "fetch_time", "close",
}
TRADE_METHODS = {"create_order", "cancel_order", "cancel_all_orders"}


class LiveTradingDisabled(PermissionError):
    pass


class OrderValidationError(ValueError):
    pass


class SafeExchange:
    """Wraps a ccxt exchange instance and whitelists the methods that may be called."""

    def __init__(self, exchange, allow_trading: bool):
        object.__setattr__(self, "_ex", exchange)
        object.__setattr__(self, "_allow_trading", allow_trading)

    def __getattr__(self, name):
        if name in FORBIDDEN_METHODS or "withdraw" in name.lower() or name.lower().startswith("transfer"):
            raise LiveTradingDisabled(f"method '{name}' is permanently forbidden in Arbies")
        if name in READ_ONLY_METHODS:
            return getattr(self._ex, name)
        if name in TRADE_METHODS:
            if not self._allow_trading:
                raise LiveTradingDisabled(f"method '{name}' requires trade permission (client is read-only)")
            return getattr(self._ex, name)
        if name in ("id", "markets", "has", "options"):
            return getattr(self._ex, name)
        raise LiveTradingDisabled(f"method '{name}' is not whitelisted")

    def __setattr__(self, name, value):
        raise AttributeError("SafeExchange is immutable")


@dataclass
class ArmingState:
    armed: bool = False
    armed_at: datetime | None = None
    expires_at: datetime | None = None
    exchanges: list[str] = field(default_factory=list)
    preview_id: str | None = None
    preview_created: datetime | None = None
    preview: dict | None = None


class LiveTradingGate:
    ARM_DURATION = timedelta(hours=1)
    PREVIEW_TTL = timedelta(minutes=10)

    def __init__(self, env_enabled: bool, confirmation_phrase: str):
        self.env_enabled = env_enabled
        self.phrase = confirmation_phrase
        self.state = ArmingState()

    def create_preview(self, exchanges: list[str], balances: dict, limits: dict, credentials: dict[str, str]) -> dict:
        """Step 1 of arming: produce the summary the user must review."""
        pid = uuid.uuid4().hex
        preview = {
            "preview_id": pid,
            "exchanges": exchanges,
            "balances": balances,
            "risk_limits": limits,
            "credential_permissions": credentials,
            "env_enabled": self.env_enabled,
            "risks": [
                "Real money will be used. Losses are possible and are not limited to estimated costs.",
                "Arbitrage legs can fail independently, leaving unhedged positions.",
                "Exchange outages, latency and rate limits can prevent hedging.",
                "Backtest and paper results do not guarantee live results.",
            ],
            "confirmation_phrase": self.phrase,
        }
        self.state.preview_id = pid
        self.state.preview_created = utcnow()
        self.state.preview = preview
        return preview

    def arm(self, preview_id: str, phrase: str, password_ok: bool, credentials: dict[str, str]) -> ArmingState:
        """Step 2 of arming. Raises LiveTradingDisabled unless every condition holds."""
        if not self.env_enabled:
            raise LiveTradingDisabled("live trading is disabled in the backend environment (ARBIES_LIVE_TRADING_ENABLED)")
        if not password_ok:
            raise LiveTradingDisabled("password re-authentication failed")
        st = self.state
        if st.preview_id is None or preview_id != st.preview_id or st.preview_created is None:
            raise LiveTradingDisabled("unknown or missing preview; review the live trading preview first")
        if utcnow() - st.preview_created > self.PREVIEW_TTL:
            raise LiveTradingDisabled("preview expired; create a new one")
        if phrase != self.phrase:
            raise LiveTradingDisabled("confirmation phrase does not match")
        exchanges = st.preview["exchanges"] if st.preview else []
        missing = [ex for ex in exchanges if credentials.get(ex) != "trade"]
        if missing:
            raise LiveTradingDisabled(f"no trade-permission credentials for: {', '.join(missing)}")
        now = utcnow()
        st.armed, st.armed_at, st.expires_at, st.exchanges = True, now, now + self.ARM_DURATION, list(exchanges)
        st.preview_id = None
        return st

    def disarm(self) -> None:
        self.state = ArmingState()

    def is_armed(self, exchange: str | None = None) -> bool:
        st = self.state
        if not (self.env_enabled and st.armed and st.expires_at and utcnow() < st.expires_at):
            return False
        return exchange is None or exchange in st.exchanges

    def assert_can_trade(self, exchange: str) -> None:
        if not self.env_enabled:
            raise LiveTradingDisabled("live trading disabled by environment")
        if not self.is_armed(exchange):
            raise LiveTradingDisabled(f"live trading is not armed for {exchange}")

    def status(self) -> dict:
        st = self.state
        return {
            "env_enabled": self.env_enabled,
            "armed": self.is_armed(),
            "armed_at": st.armed_at.isoformat() if st.armed_at else None,
            "expires_at": st.expires_at.isoformat() if st.expires_at else None,
            "exchanges": st.exchanges,
            "pending_preview": st.preview_id is not None,
        }


@dataclass
class LiveOrderRequest:
    exchange: str
    symbol: str
    side: Side
    amount: Decimal
    price: Decimal | None  # None = market order (discouraged; IOC limit preferred)
    order_type: str = "limit"
    time_in_force: str = "IOC"
    opportunity_key: str | None = None
    client_order_id: str = field(default_factory=lambda: "arb" + uuid.uuid4().hex[:20])


def validate_order(req: LiveOrderRequest, rules: MarketRules | None, max_notional_quote: Decimal | None = None) -> None:
    """Pre-validate an order against exchange rules. Raises OrderValidationError."""
    if req.amount <= 0:
        raise OrderValidationError("amount must be positive")
    if req.order_type == "limit" and (req.price is None or req.price <= 0):
        raise OrderValidationError("limit orders need a positive price")
    if rules is None:
        raise OrderValidationError(f"no market rules for {req.exchange} {req.symbol}")
    if not rules.active:
        raise OrderValidationError("market inactive")
    if rules.amount_step and rules.round_amount(req.amount) != req.amount:
        raise OrderValidationError(f"amount {req.amount} violates lot size {rules.amount_step}")
    if rules.price_step and req.price is not None and (req.price / rules.price_step) % 1 != 0:
        raise OrderValidationError(f"price {req.price} violates tick size {rules.price_step}")
    ref_price = req.price or Decimal(0)
    if not rules.meets_minimums(req.amount, ref_price if ref_price > 0 else Decimal(1)):
        raise OrderValidationError("order below exchange minimum amount/cost")
    if max_notional_quote is not None and req.price is not None and req.amount * req.price > max_notional_quote:
        raise OrderValidationError("order notional exceeds the per-order limit")


CLIENT_ID_PARAM = {"kraken": "cl_ord_id", "coinbase": "client_order_id", "bitstamp": "client_order_id",
                   "gemini": "client_order_id"}


class LiveOrderManager:
    """Submits, tracks and cancels live orders through a SafeExchange.

    The order book of record is kept by the caller (persisted as LiveOrder rows);
    this class enforces gating, validation and idempotency."""

    def __init__(self, gate: LiveTradingGate, risk: RiskEngine, clients: dict[str, SafeExchange],
                 rules: dict[tuple[str, str], MarketRules], audit=None):
        self.gate = gate
        self.risk = risk
        self.clients = clients
        self.rules = rules
        self.audit = audit or (lambda action, detail: None)
        self.orders: dict[str, dict] = {}

    async def submit(self, req: LiveOrderRequest, max_notional_quote: Decimal | None = None) -> dict:
        self.gate.assert_can_trade(req.exchange)
        if self.risk.kill_switch:
            raise LiveTradingDisabled("kill switch active")
        if self.risk.breaker_active(utcnow()):
            raise LiveTradingDisabled("circuit breaker active")
        validate_order(req, self.rules.get((req.exchange, req.symbol)), max_notional_quote)
        if not self.risk.register_client_order_id(req.client_order_id):
            raise OrderValidationError("duplicate client order id")
        client = self.clients.get(req.exchange)
        if client is None:
            raise LiveTradingDisabled(f"no live client for {req.exchange}")
        params = {CLIENT_ID_PARAM.get(req.exchange, "clientOrderId"): req.client_order_id}
        if req.order_type == "limit" and req.time_in_force:
            params["timeInForce"] = req.time_in_force
        self.audit("live_order_submit", {"client_order_id": req.client_order_id, "exchange": req.exchange,
                                         "symbol": req.symbol, "side": req.side.value, "amount": str(req.amount),
                                         "price": str(req.price) if req.price else None})
        res = await client.create_order(req.symbol, req.order_type, req.side.value, float(req.amount),
                                        float(req.price) if req.price is not None else None, params)
        self.orders[req.client_order_id] = {"request": req, "response": res}
        return res

    async def cancel(self, exchange: str, order_id: str, symbol: str) -> dict:
        client = self.clients.get(exchange)
        if client is None:
            raise LiveTradingDisabled(f"no live client for {exchange}")
        self.audit("live_order_cancel", {"exchange": exchange, "order_id": order_id, "symbol": symbol})
        return await client.cancel_order(order_id, symbol)

    async def emergency_cancel_all(self) -> dict[str, str]:
        """Cancel every open order on every connected exchange (allowed even when disarmed)."""
        results = {}
        for ex, client in self.clients.items():
            try:
                open_orders = await client.fetch_open_orders()
                for o in open_orders:
                    await self._raw_cancel(client, o["id"], o["symbol"])
                results[ex] = f"cancelled {len(open_orders)}"
            except LiveTradingDisabled as e:
                results[ex] = f"skipped: {e}"
            except Exception as e:  # noqa: BLE001
                results[ex] = f"error: {e}"
        self.audit("emergency_cancel_all", results)
        return results

    @staticmethod
    async def _raw_cancel(client: SafeExchange, order_id: str, symbol: str):
        return await client.cancel_order(order_id, symbol)


def reconcile_balances(expected: dict[tuple[str, str], Decimal], actual: dict[tuple[str, str], Decimal],
                       tolerance: Decimal = Decimal("0.00000001")) -> list[dict]:
    """Compare internally expected balances with exchange-reported balances."""
    diffs = []
    for key in sorted(set(expected) | set(actual)):
        e = expected.get(key, Decimal(0))
        a = actual.get(key, Decimal(0))
        if abs(e - a) > tolerance:
            diffs.append({"exchange": key[0], "asset": key[1], "expected": str(e), "actual": str(a),
                          "difference": str(a - e)})
    return diffs
