"""Core normalised market data structures.

All monetary quantities are ``Decimal``. All timestamps are timezone-aware UTC
``datetime`` objects. Exchange-specific details are preserved in ``raw``/``info``
fields where useful.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from enum import StrEnum

ZERO = Decimal("0")


def utcnow() -> datetime:
    return datetime.now(UTC)


def D(value: object) -> Decimal:
    """Convert a value to Decimal without binary float artefacts."""
    if isinstance(value, Decimal):
        return value
    if value is None:
        return ZERO
    return Decimal(str(value))


class DataSource(StrEnum):
    """Provenance of market data. Never mix these silently."""

    LIVE = "live_recorded"  # recorded by Arbies from a real exchange feed
    IMPORTED = "third_party_imported"  # imported from an external historical provider
    SYNTHETIC = "synthetic"  # generated test data - never a real observation


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    amount: Decimal


@dataclass(slots=True)
class OrderBook:
    exchange: str
    symbol: str
    bids: list[BookLevel]  # sorted best (highest) first
    asks: list[BookLevel]  # sorted best (lowest) first
    received_ts: datetime  # local receive time (UTC)
    exchange_ts: datetime | None = None  # exchange-provided timestamp (UTC) if any
    source: DataSource = DataSource.LIVE
    sequence: int | None = None

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread_pct(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None or self.best_ask == 0:
            return None
        return (self.best_ask - self.best_bid) / self.best_ask * 100

    def age_ms(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        return (now - self.received_ts).total_seconds() * 1000.0

    def is_crossed(self) -> bool:
        return self.best_bid is not None and self.best_ask is not None and self.best_bid >= self.best_ask

    def truncated(self, depth: int) -> OrderBook:
        return OrderBook(
            exchange=self.exchange,
            symbol=self.symbol,
            bids=self.bids[:depth],
            asks=self.asks[:depth],
            received_ts=self.received_ts,
            exchange_ts=self.exchange_ts,
            source=self.source,
            sequence=self.sequence,
        )

    def to_dict(self, depth: int | None = None) -> dict:
        bids = self.bids if depth is None else self.bids[:depth]
        asks = self.asks if depth is None else self.asks[:depth]
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "bids": [[str(lv.price), str(lv.amount)] for lv in bids],
            "asks": [[str(lv.price), str(lv.amount)] for lv in asks],
            "received_ts": self.received_ts.isoformat(),
            "exchange_ts": self.exchange_ts.isoformat() if self.exchange_ts else None,
            "source": self.source.value,
            "sequence": self.sequence,
        }

    @staticmethod
    def from_levels(
        exchange: str,
        symbol: str,
        bids: list[list] | list[tuple],
        asks: list[list] | list[tuple],
        received_ts: datetime,
        exchange_ts: datetime | None = None,
        source: DataSource = DataSource.LIVE,
        sequence: int | None = None,
    ) -> OrderBook:
        b = [BookLevel(D(p), D(a)) for p, a, *_ in bids if D(a) > 0]
        s = [BookLevel(D(p), D(a)) for p, a, *_ in asks if D(a) > 0]
        b.sort(key=lambda lv: lv.price, reverse=True)
        s.sort(key=lambda lv: lv.price)
        return OrderBook(exchange, symbol, b, s, received_ts, exchange_ts, source, sequence)


@dataclass(slots=True)
class MarketRules:
    """Exchange-specific trading rules for a market."""

    exchange: str
    symbol: str
    base: str
    quote: str
    taker_fee: Decimal = Decimal("0.0026")
    maker_fee: Decimal = Decimal("0.0016")
    amount_step: Decimal | None = None  # lot size, e.g. 0.00000001
    price_step: Decimal | None = None
    min_amount: Decimal | None = None
    min_cost: Decimal | None = None
    active: bool = True
    fee_source: str = "default"  # "exchange", "config" or "default"
    info: dict = field(default_factory=dict)

    def round_amount(self, amount: Decimal) -> Decimal:
        if self.amount_step and self.amount_step > 0:
            return (amount / self.amount_step).to_integral_value(rounding=ROUND_DOWN) * self.amount_step
        return amount

    def meets_minimums(self, amount: Decimal, price: Decimal) -> bool:
        if self.min_amount is not None and amount < self.min_amount:
            return False
        if self.min_cost is not None and amount * price < self.min_cost:
            return False
        return amount > 0


def split_symbol(symbol: str) -> tuple[str, str]:
    base, _, quote = symbol.partition("/")
    quote = quote.split(":")[0]
    return base, quote
