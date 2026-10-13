"""Multi-exchange inventory accounting.

Balances are tracked per (exchange, asset). Cash and crypto inventories are
tracked separately and a spend greater than the available balance raises
``InsufficientBalance`` - the simulator can never spend money it does not have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from arbies.core.models import ZERO, Side, split_symbol
from arbies.core.valuation import Valuator


class InsufficientBalance(Exception):
    def __init__(self, exchange: str, asset: str, required: Decimal, available: Decimal):
        super().__init__(f"insufficient {asset} on {exchange}: need {required}, have {available}")
        self.exchange = exchange
        self.asset = asset
        self.required = required
        self.available = available


@dataclass(slots=True)
class LedgerEntry:
    ts: datetime
    exchange: str
    asset: str
    delta: Decimal
    balance_after: Decimal
    kind: str  # deposit | trade | fee | transfer_out | transfer_in | transfer_fee
    ref: str = ""

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat(), "exchange": self.exchange, "asset": self.asset,
            "delta": str(self.delta), "balance_after": str(self.balance_after), "kind": self.kind, "ref": self.ref,
        }


@dataclass
class Portfolio:
    balances: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    ledger: list[LedgerEntry] = field(default_factory=list)
    keep_ledger: bool = True
    in_transit: list[tuple[datetime, str, str, Decimal]] = field(default_factory=list)  # (arrival, ex, asset, amt)

    # ------------------------------------------------------------------ queries
    def balance(self, exchange: str, asset: str) -> Decimal:
        return self.balances.get((exchange, asset), ZERO)

    def available(self, exchange: str, asset: str) -> Decimal:
        return self.balance(exchange, asset)

    def assets(self) -> set[str]:
        return {a for (_e, a) in self.balances} | {a for (_t, _e, a, _x) in self.in_transit}

    def exchanges(self) -> set[str]:
        return {e for (e, _a) in self.balances}

    def copy(self) -> Portfolio:
        return Portfolio(dict(self.balances), [], self.keep_ledger, list(self.in_transit))

    # ------------------------------------------------------------------ mutations
    def _move(self, ts: datetime, exchange: str, asset: str, delta: Decimal, kind: str, ref: str) -> None:
        cur = self.balance(exchange, asset)
        new = cur + delta
        if new < 0:
            raise InsufficientBalance(exchange, asset, -delta, cur)
        self.balances[(exchange, asset)] = new
        if self.keep_ledger:
            self.ledger.append(LedgerEntry(ts, exchange, asset, delta, new, kind, ref))

    def deposit(self, ts: datetime, exchange: str, asset: str, amount: Decimal, ref: str = "initial") -> None:
        self._move(ts, exchange, asset, amount, "deposit", ref)

    def can_trade(self, exchange: str, symbol: str, side: Side, base_amount: Decimal, quote_amount: Decimal,
                  fee: Decimal, fee_currency: str) -> bool:
        base, quote = split_symbol(symbol)
        need: dict[str, Decimal] = {}
        if side == Side.BUY:
            need[quote] = quote_amount
        else:
            need[base] = base_amount
        recv_ccy = base if side == Side.BUY else quote
        if fee_currency != recv_ccy:
            need[fee_currency] = need.get(fee_currency, ZERO) + fee
        return all(self.available(exchange, a) >= amt for a, amt in need.items())

    def apply_trade(self, ts: datetime, exchange: str, symbol: str, side: Side, base_amount: Decimal,
                    quote_amount: Decimal, fee: Decimal, fee_currency: str, ref: str = "") -> None:
        """Atomically apply a fill. Raises InsufficientBalance without modifying state."""
        if not self.can_trade(exchange, symbol, side, base_amount, quote_amount, fee, fee_currency):
            base, quote = split_symbol(symbol)
            spend_asset = quote if side == Side.BUY else base
            spend_amt = quote_amount if side == Side.BUY else base_amount
            raise InsufficientBalance(exchange, spend_asset, spend_amt, self.available(exchange, spend_asset))
        base, quote = split_symbol(symbol)
        if side == Side.BUY:
            self._move(ts, exchange, quote, -quote_amount, "trade", ref)
            self._move(ts, exchange, base, base_amount, "trade", ref)
        else:
            self._move(ts, exchange, base, -base_amount, "trade", ref)
            self._move(ts, exchange, quote, quote_amount, "trade", ref)
        if fee:
            self._move(ts, exchange, fee_currency, -fee, "fee", ref)

    def start_transfer(self, ts: datetime, asset: str, src: str, dst: str, amount: Decimal, fee: Decimal,
                       arrival: datetime, ref: str = "rebalance") -> None:
        if self.available(src, asset) < amount + fee:
            raise InsufficientBalance(src, asset, amount + fee, self.available(src, asset))
        self._move(ts, src, asset, -amount, "transfer_out", ref)
        if fee:
            self._move(ts, src, asset, -fee, "transfer_fee", ref)
        self.in_transit.append((arrival, dst, asset, amount))

    def settle_transfers(self, now: datetime) -> int:
        settled = [t for t in self.in_transit if t[0] <= now]
        self.in_transit = [t for t in self.in_transit if t[0] > now]
        for arrival, ex, asset, amt in settled:
            self._move(arrival, ex, asset, amt, "transfer_in", "rebalance")
        return len(settled)

    # ------------------------------------------------------------------ valuation
    def total_value(self, valuator: Valuator) -> tuple[Decimal, bool]:
        total = ZERO
        estimated = False
        for (_ex, asset), amt in self.balances.items():
            v, est = valuator.value(asset, amt)
            if v is None:
                estimated = True
                continue
            total += v
            estimated = estimated or est
        for _arr, _ex, asset, amt in self.in_transit:
            v, est = valuator.value(asset, amt)
            total += v or ZERO
            estimated = estimated or est or v is None
        return total, estimated

    def value_by_exchange(self, valuator: Valuator) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for (ex, asset), amt in self.balances.items():
            out[ex] = out.get(ex, ZERO) + valuator.to_reporting(asset, amt)
        return out

    def value_by_asset(self, valuator: Valuator) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for (_ex, asset), amt in self.balances.items():
            out[asset] = out.get(asset, ZERO) + valuator.to_reporting(asset, amt)
        return out

    def snapshot(self, valuator: Valuator | None = None) -> list[dict]:
        rows = []
        for (ex, asset), amt in sorted(self.balances.items()):
            row = {"exchange": ex, "asset": asset, "amount": str(amt)}
            if valuator is not None:
                v, est = valuator.value(asset, amt)
                row["value"] = str(v) if v is not None else None
                row["estimated"] = est
            rows.append(row)
        return rows
