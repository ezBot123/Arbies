"""Market data adapter interfaces and connection health tracking."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from arbies.core.models import MarketRules, OrderBook, utcnow


class ConnStatus(StrEnum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"  # some symbols failing or using REST fallback
    DISCONNECTED = "disconnected"
    ERROR = "error"


@dataclass
class ExchangeHealth:
    exchange: str
    status: ConnStatus = ConnStatus.CONNECTING
    transport: str = "websocket"  # websocket | rest | synthetic
    symbols: list[str] = field(default_factory=list)
    unavailable_symbols: list[str] = field(default_factory=list)
    last_update: dict[str, datetime] = field(default_factory=dict)
    updates: int = 0
    errors: int = 0
    reconnects: int = 0
    last_error: str = ""
    last_error_at: datetime | None = None
    clock_offset_ms: float | None = None  # exchange time - local time
    connected_since: datetime | None = None
    latency_ms_ewma: float | None = None  # receive_ts - exchange_ts, smoothed (includes clock offset)

    def fresh(self, now: datetime, max_age_ms: int) -> bool:
        if not self.last_update:
            return False
        newest = max(self.last_update.values())
        return (now - newest).total_seconds() * 1000 <= max_age_ms

    def stale_symbols(self, now: datetime, max_age_ms: int) -> list[str]:
        out = []
        for s in self.symbols:
            t = self.last_update.get(s)
            if t is None or (now - t).total_seconds() * 1000 > max_age_ms:
                out.append(s)
        return out

    def to_dict(self, now: datetime | None = None, max_age_ms: int = 5000) -> dict:
        now = now or utcnow()
        return {
            "exchange": self.exchange,
            "status": self.status.value,
            "transport": self.transport,
            "symbols": self.symbols,
            "unavailable_symbols": self.unavailable_symbols,
            "stale_symbols": self.stale_symbols(now, max_age_ms),
            "last_update": {s: t.isoformat() for s, t in self.last_update.items()},
            "updates": self.updates,
            "errors": self.errors,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at.isoformat() if self.last_error_at else None,
            "clock_offset_ms": self.clock_offset_ms,
            "latency_ms_ewma": round(self.latency_ms_ewma, 1) if self.latency_ms_ewma is not None else None,
            "connected_since": self.connected_since.isoformat() if self.connected_since else None,
            "healthy": self.status in (ConnStatus.CONNECTED, ConnStatus.DEGRADED) and self.fresh(now, max_age_ms),
        }


BookCallback = Callable[[OrderBook], None]
EventCallback = Callable[[str, str | None, str, str], None]  # exchange, symbol, event, detail


class ExchangeAdapter:
    """Base class for market data adapters."""

    exchange_id: str
    health: ExchangeHealth

    async def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def stop(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def market_rules(self) -> dict[str, MarketRules]:  # pragma: no cover - interface
        raise NotImplementedError
