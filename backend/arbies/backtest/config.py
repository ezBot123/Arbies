"""Backtest configuration. A saved config + dataset id + code revision + seed is
sufficient to reproduce a result exactly."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from arbies.arbitrage.models import StrategyParams
from arbies.core.models import DataSource, split_symbol
from arbies.execution.simulator import FillModel
from arbies.risk.engine import RiskLimits


class RebalanceConfig(BaseModel):
    enabled: bool = True
    threshold_pct: Decimal = Decimal("80")  # rebalance when one exchange holds > X% of an asset
    target_pct: Decimal = Decimal("50")  # move towards an even split
    check_interval_s: int = 600
    transfer_delay_s: int = 1800  # time in transit (no access to funds)
    transfer_fee_bps: Decimal = Decimal("0")
    withdrawal_fees: dict[str, Decimal] = Field(default_factory=lambda: {
        # flat per-withdrawal fees in asset units - placeholders, verify per exchange
        "BTC": Decimal("0.0001"), "ETH": Decimal("0.0025"), "USD": Decimal("5"), "GBP": Decimal("1"),
        "USDC": Decimal("2"), "USDT": Decimal("3"), "EUR": Decimal("1"),
    })


class AllocationItem(BaseModel):
    exchange: str
    asset: str
    fraction: Decimal  # fraction of initial capital (GBP) allocated to this bucket


class BacktestConfig(BaseModel):
    name: str = ""
    strategies: list[str] = Field(default_factory=lambda: ["cross_exchange"])  # cross_exchange, triangular
    exchanges: list[str] = Field(default_factory=lambda: ["kraken", "coinbase"])
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USD", "ETH/USD"])
    start: datetime
    end: datetime
    data_source: DataSource = DataSource.LIVE
    initial_capital_gbp: Decimal = Decimal("500")
    allocation: list[AllocationItem] | None = None  # None -> default even allocation
    params: StrategyParams = Field(default_factory=StrategyParams)
    fill: FillModel = Field(default_factory=FillModel)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    rebalance: RebalanceConfig = Field(default_factory=RebalanceConfig)
    decision_interval_ms: int = 500
    equity_sample_interval_s: int = 60
    max_concurrent_jobs_per_exchange: int = 1
    seed: int = 42

    @model_validator(mode="after")
    def _check(self):
        if self.end <= self.start:
            raise ValueError("end must be after start")
        for s in self.strategies:
            if s not in ("cross_exchange", "triangular"):
                raise ValueError(f"unknown strategy {s}")
        if self.allocation is not None:
            total = sum((a.fraction for a in self.allocation), Decimal(0))
            if total > Decimal("1.0000001"):
                raise ValueError("allocation fractions sum to more than 1")
        self.fill.seed = self.seed
        return self

    def resolved_allocation(self) -> list[AllocationItem]:
        """Default: capital split evenly across exchanges; on each exchange half in the
        primary quote currency and half spread across base assets."""
        if self.allocation is not None:
            return self.allocation
        quotes: list[str] = []
        bases: list[str] = []
        for s in self.symbols:
            b, q = split_symbol(s)
            if q not in quotes:
                quotes.append(q)
            if b not in bases:
                bases.append(b)
        fiat = [q for q in quotes if q in ("USD", "GBP", "EUR", "USDC", "USDT")] or quotes[:1]
        crypto = [b for b in bases if b not in ("USD", "GBP", "EUR", "USDC", "USDT")]
        n_ex = len(self.exchanges)
        out = []
        for ex in self.exchanges:
            share = Decimal(1) / n_ex
            cash_share = share / 2 if crypto else share
            for f in fiat:  # cash spread across every fiat quote currency in use
                out.append(AllocationItem(exchange=ex, asset=f, fraction=cash_share / len(fiat)))
            for c in crypto:
                out.append(AllocationItem(exchange=ex, asset=c, fraction=share / 2 / len(crypto)))
        return out
