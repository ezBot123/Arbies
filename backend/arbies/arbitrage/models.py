"""Opportunity and strategy parameter models shared by all detectors."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from arbies.core.models import ZERO, Side


class StrategyKind(StrEnum):
    CROSS_EXCHANGE = "cross_exchange"
    TRIANGULAR = "triangular"


class RejectionReason(StrEnum):
    SPREAD_NON_POSITIVE = "spread_non_positive"  # no gross price discrepancy at all
    FEES_EXCEED_PROFIT = "fees_exceed_profit"  # gross positive but costs make it negative
    BELOW_THRESHOLD = "below_threshold"  # net positive but under min profit threshold
    INSUFFICIENT_LIQUIDITY = "insufficient_liquidity"
    INSUFFICIENT_INVENTORY = "insufficient_inventory"
    BELOW_MIN_ORDER = "below_min_order"
    STALE_DATA = "stale_data"
    EXCHANGE_UNAVAILABLE = "exchange_unavailable"
    RISK_LIMIT = "risk_limit"
    NO_VALUATION = "no_valuation"


REJECTION_EXPLANATIONS: dict[str, str] = {
    RejectionReason.SPREAD_NON_POSITIVE: "Spread too small: the best buy price is not below the best sell price.",
    RejectionReason.FEES_EXCEED_PROFIT: "Fees and modelled costs exceed the gross spread.",
    RejectionReason.BELOW_THRESHOLD: "Net expected return is positive but below the minimum profit threshold.",
    RejectionReason.INSUFFICIENT_LIQUIDITY: "Insufficient order-book liquidity for a profitable trade size.",
    RejectionReason.INSUFFICIENT_INVENTORY: "Insufficient balance on the required exchange(s).",
    RejectionReason.BELOW_MIN_ORDER: "Trade size is below the exchange minimum order size or cost.",
    RejectionReason.STALE_DATA: "Market data is older than the maximum permitted age.",
    RejectionReason.EXCHANGE_UNAVAILABLE: "Exchange connection is unhealthy or unavailable.",
    RejectionReason.RISK_LIMIT: "A deterministic risk limit would be exceeded.",
    RejectionReason.NO_VALUATION: "Unable to value the trade in the reporting currency.",
}


class StrategyParams(BaseModel):
    """Parameters for opportunity evaluation. Defaults are research placeholders,
    not claims of profitability."""

    min_net_profit_pct: Decimal = Decimal("0.30")
    max_trade_gbp: Decimal = Decimal("25")
    max_data_age_ms: int = 5000
    slippage_buffer_bps: Decimal = Decimal("5")  # extra adverse price movement assumption
    latency_ms: int = 250  # expected decision-to-fill latency
    latency_cost_bps: Decimal = Decimal("2")  # expected adverse movement over the latency window
    rebalancing_cost_bps: Decimal = Decimal("10")  # amortised cost of moving inventory back
    fx_cost_bps: Decimal = Decimal("10")  # when comparing equivalent but different quote assets
    leg_failure_prob: Decimal = Decimal("0.02")  # probability one leg fails/partially fills
    leg_failure_loss_bps: Decimal = Decimal("50")  # loss on the unhedged notional if it does
    quote_equivalents: list[list[str]] = Field(default_factory=list)  # e.g. [["USD","USDC"]]
    start_currencies: list[str] = Field(default_factory=lambda: ["USD", "GBP", "USDC", "USDT", "EUR"])
    fee_overrides: dict[str, Decimal] = Field(default_factory=dict)  # "exchange" or "exchange:SYMBOL" -> taker fee
    triangular_size_steps: int = 6

    def taker_fee(self, exchange: str, symbol: str, default: Decimal) -> Decimal:
        return self.fee_overrides.get(f"{exchange}:{symbol}", self.fee_overrides.get(exchange, default))


@dataclass(slots=True)
class Leg:
    exchange: str
    symbol: str
    side: Side
    base_amount: Decimal
    vwap: Decimal
    quote_amount: Decimal
    fee: Decimal  # in the currency received
    fee_currency: str
    top_price: Decimal
    levels_used: int

    def to_dict(self) -> dict:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "side": self.side.value,
            "base_amount": str(self.base_amount),
            "vwap": str(self.vwap),
            "quote_amount": str(self.quote_amount),
            "fee": str(self.fee),
            "fee_currency": self.fee_currency,
            "top_price": str(self.top_price),
            "levels_used": self.levels_used,
        }


@dataclass(slots=True)
class Opportunity:
    kind: StrategyKind
    key: str  # stable identity of the route (used for age tracking & dedupe)
    exchanges: list[str]
    symbols: list[str]
    detected_at: datetime
    profit_currency: str  # currency in which gross/net profit is denominated
    legs: list[Leg] = field(default_factory=list)
    route: list[str] = field(default_factory=list)  # e.g. ["USD","BTC","ETH","USD"]
    buy_exchange: str | None = None
    sell_exchange: str | None = None
    size_base: Decimal = ZERO  # cross-exchange: base amount; triangular: start currency amount
    notional: Decimal = ZERO  # in profit_currency
    gross_spread_pct: Decimal = ZERO  # top-of-book discrepancy, informational
    gross_profit: Decimal = ZERO  # executable (depth-walked) profit before any costs
    fees: Decimal = ZERO
    depth_slippage: Decimal = ZERO  # cost of walking the book vs top-of-book (already in gross)
    slippage_buffer: Decimal = ZERO
    latency_cost: Decimal = ZERO
    rebalancing_cost: Decimal = ZERO
    fx_cost: Decimal = ZERO
    leg_failure_cost: Decimal = ZERO  # expected cost of one leg failing (probability x loss)
    net_profit: Decimal = ZERO
    net_return_pct: Decimal = ZERO
    net_profit_gbp: Decimal | None = None
    notional_gbp: Decimal | None = None
    max_size_base: Decimal = ZERO  # max size supported by depth/inventory before costs
    data_age_ms: float = 0.0
    confidence: float = 1.0  # 0..1 data quality score
    qualified: bool = False
    rejection_reasons: list[RejectionReason] = field(default_factory=list)
    first_seen: datetime | None = None
    data_source: str = "live_recorded"

    @staticmethod
    def make_key(kind: StrategyKind, parts: list[str]) -> str:
        return f"{kind.value}:" + "|".join(parts)

    @property
    def id(self) -> str:
        h = hashlib.sha1(f"{self.key}@{self.detected_at.isoformat()}".encode()).hexdigest()
        return h[:16]

    @property
    def total_costs(self) -> Decimal:
        return (self.fees + self.slippage_buffer + self.latency_cost + self.rebalancing_cost
                + self.fx_cost + self.leg_failure_cost)

    def age_ms(self, now: datetime) -> float:
        start = self.first_seen or self.detected_at
        return (now - start).total_seconds() * 1000

    def reject(self, reason: RejectionReason) -> None:
        if reason not in self.rejection_reasons:
            self.rejection_reasons.append(reason)
        self.qualified = False

    def to_dict(self, now: datetime | None = None) -> dict:
        now = now or self.detected_at
        return {
            "id": self.id,
            "key": self.key,
            "kind": self.kind.value,
            "exchanges": self.exchanges,
            "symbols": self.symbols,
            "route": self.route,
            "buy_exchange": self.buy_exchange,
            "sell_exchange": self.sell_exchange,
            "detected_at": self.detected_at.isoformat(),
            "first_seen": (self.first_seen or self.detected_at).isoformat(),
            "age_ms": round(self.age_ms(now), 1),
            "profit_currency": self.profit_currency,
            "size_base": str(self.size_base),
            "notional": str(self.notional),
            "notional_gbp": str(self.notional_gbp) if self.notional_gbp is not None else None,
            "gross_spread_pct": str(self.gross_spread_pct),
            "gross_profit": str(self.gross_profit),
            "fees": str(self.fees),
            "depth_slippage": str(self.depth_slippage),
            "slippage_buffer": str(self.slippage_buffer),
            "latency_cost": str(self.latency_cost),
            "rebalancing_cost": str(self.rebalancing_cost),
            "fx_cost": str(self.fx_cost),
            "leg_failure_cost": str(self.leg_failure_cost),
            "total_costs": str(self.total_costs),
            "net_profit": str(self.net_profit),
            "net_profit_gbp": str(self.net_profit_gbp) if self.net_profit_gbp is not None else None,
            "net_return_pct": str(self.net_return_pct),
            "max_size_base": str(self.max_size_base),
            "data_age_ms": round(self.data_age_ms, 1),
            "confidence": round(self.confidence, 3),
            "qualified": self.qualified,
            "rejection_reasons": [r.value for r in self.rejection_reasons],
            "rejection_explanations": [REJECTION_EXPLANATIONS[r] for r in self.rejection_reasons],
            "legs": [leg.to_dict() for leg in self.legs],
            "data_source": self.data_source,
        }


# (exchange, asset) -> available balance, or None if inventory is not constrained
BalanceFn = Callable[[str, str], Decimal | None]
# exchange -> healthy?
HealthFn = Callable[[str], bool]


def confidence_from_age(age_ms: float, max_age_ms: int) -> float:
    if max_age_ms <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - age_ms / max_age_ms))
