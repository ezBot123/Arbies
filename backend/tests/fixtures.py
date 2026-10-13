"""Synthetic fixtures for unit tests. These are NOT real market observations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from arbies.core.models import DataSource, MarketRules, OrderBook
from arbies.core.valuation import Valuator

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def book(exchange, symbol, bids, asks, ts=T0, source=DataSource.SYNTHETIC) -> OrderBook:
    return OrderBook.from_levels(exchange, symbol, bids, asks, received_ts=ts, source=source)


def valuator(prices: dict[str, str] | None = None) -> Valuator:
    v = Valuator("GBP")
    v.set_edges({k: Decimal(p) for k, p in (prices or {"USD/GBP": "0.8", "BTC/USD": "50000", "ETH/USD": "2500"}).items()})
    return v


def rules(exchange, symbol, taker="0.001", step="0.00000001", min_amount=None, min_cost=None) -> MarketRules:
    base, quote = symbol.split("/")
    return MarketRules(exchange, symbol, base, quote, taker_fee=Decimal(taker), maker_fee=Decimal(taker),
                       amount_step=Decimal(step) if step else None,
                       min_amount=Decimal(min_amount) if min_amount else None,
                       min_cost=Decimal(min_cost) if min_cost else None)


def ts(ms: int) -> datetime:
    return T0 + timedelta(milliseconds=ms)
