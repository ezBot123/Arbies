"""Personal contribution plans, dated exchange fee schedules and contribution-aware return measures.

Deposits are capital flows, never trading income: every report built on these helpers keeps
``contributions`` separate from trading P&L and from market (inventory) P&L.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

ZERO = Decimal(0)


# ============================================================================ contributions
class ContributionPlan(BaseModel):
    """Initial capital plus a recurring deposit, all in ``currency`` (GBP by default)."""

    initial_amount: Decimal = Decimal("500")
    recurring_amount: Decimal = Decimal("100")
    frequency: str = "monthly"  # monthly | weekly | none
    day_of_month: int = Field(1, ge=1, le=28)
    first_contribution: date | None = None  # default: the first scheduled date after the start date
    last_contribution: date | None = None  # default: the last scheduled date on or before the end date
    currency: str = "GBP"
    conversion_cost_bps: Decimal = Decimal("50")  # fee + spread when deposits are converted into other assets
    reinvest_profits: bool = True  # realised profits stay in the portfolio (no withdrawals are modelled)

    @model_validator(mode="after")
    def _check(self):
        if self.frequency not in ("monthly", "weekly", "none"):
            raise ValueError("frequency must be monthly, weekly or none")
        if self.initial_amount < 0 or self.recurring_amount < 0:
            raise ValueError("contribution amounts cannot be negative")
        if not self.reinvest_profits:
            raise ValueError("profit withdrawal is not modelled; reinvest_profits must be true")
        return self

    def schedule(self, start: datetime, end: datetime) -> list[tuple[datetime, Decimal, str]]:
        """All deposits in [start, end] as (timestamp UTC 00:00, amount, label). The initial deposit is at ``start``."""
        out: list[tuple[datetime, Decimal, str]] = []
        if self.initial_amount > 0:
            out.append((start, self.initial_amount, "initial"))
        if self.frequency == "none" or self.recurring_amount <= 0:
            return out
        first = self.first_contribution
        last = self.last_contribution or end.date()
        d = _next_scheduled(start.date(), self.frequency, self.day_of_month, strictly_after=True)
        if first is not None:
            d = max(d, first)
            d = _next_scheduled(d, self.frequency, self.day_of_month, strictly_after=False)
        while d <= last and datetime.combine(d, time(0), UTC) <= end:
            out.append((datetime.combine(d, time(0), UTC), self.recurring_amount, "monthly" if self.frequency == "monthly"
                        else "weekly"))
            d = _next_scheduled(d, self.frequency, self.day_of_month, strictly_after=True)
        return out

    def totals(self, start: datetime, end: datetime) -> dict:
        s = self.schedule(start, end)
        recurring = [a for _t, a, lbl in s if lbl != "initial"]
        return {"initial": str(self.initial_amount if s and s[0][2] == "initial" else ZERO),
                "recurring_count": len(recurring), "recurring_total": str(sum(recurring, ZERO)),
                "total": str(sum((a for _t, a, _l in s), ZERO)), "currency": self.currency}


def _next_scheduled(d: date, frequency: str, dom: int, strictly_after: bool) -> date:
    if frequency == "weekly":
        from datetime import timedelta
        return d + timedelta(days=7) if strictly_after else d
    # monthly on day ``dom``
    cand = d.replace(day=dom) if d.day <= dom else None
    if cand is not None and (cand > d or (cand == d and not strictly_after)):
        return cand
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return date(y, m, min(dom, calendar.monthrange(y, m)[1]))


# ============================================================================ fee schedules
class FeeEntry(BaseModel):
    exchange: str
    effective_from: date
    maker: Decimal
    taker: Decimal
    tier: str = "retail entry tier"
    verification: str = "unverified"  # official | official_blog | secondary_sources | unverified
    source: str = ""
    note: str = ""


class FeeSchedule(BaseModel):
    """Dated retail fee schedule per exchange. The most recent entry on or before a date applies."""

    name: str = "base"
    entries: list[FeeEntry] = Field(default_factory=list)

    def at(self, exchange: str, when: date | datetime) -> FeeEntry | None:
        d = when.date() if isinstance(when, datetime) else when
        cands = [e for e in self.entries if e.exchange == exchange and e.effective_from <= d]
        return max(cands, key=lambda e: e.effective_from) if cands else None

    def change_dates(self, start: date, end: date) -> list[date]:
        return sorted({e.effective_from for e in self.entries if start < e.effective_from <= end})

    def scaled(self, factor: Decimal, name: str) -> FeeSchedule:
        return FeeSchedule(name=name, entries=[e.model_copy(update={"maker": e.maker * factor, "taker": e.taker * factor,
                                                                  "note": f"{e.note} (x{factor} sensitivity)".strip()})
                                               for e in self.entries])


_D = date(2025, 1, 1)
KRAKEN_FEES = "https://www.kraken.com/features/fee-schedule"
KRAKEN_BLOG = "https://blog.kraken.com/product/pro/new-kraken-pro-fee-tiers"
COINBASE_HELP = "https://help.coinbase.com/en/pro/trading-and-funding/trading-rules-and-fees/fees"
BITSTAMP_REVIEW = "https://cryptoslate.com/crypto-exchanges/bitstamp-exchange-review/"


def default_fee_schedules() -> dict[str, FeeSchedule]:
    """Researched 2025-01-01 .. 2026-10-13 retail fee assumptions (see FEES section of BACKTESTING.md).

    ``base`` is deliberately conservative where sources conflict; ``low`` and ``high`` bracket it for
    sensitivity analysis. Nothing here is an account-specific tier.
    """
    base = FeeSchedule(name="base", entries=[
        FeeEntry(exchange="kraken", effective_from=_D, maker=Decimal("0.0025"), taker=Decimal("0.0040"),
                 tier="Kraken Pro $0-10k", verification="secondary_sources", source=KRAKEN_FEES,
                 note="pre-July-2026 entry tier per several reviews; Kraken's own page now shows only the new table"),
        FeeEntry(exchange="kraken", effective_from=date(2026, 7, 9), maker=Decimal("0.0040"), taker=Decimal("0.0080"),
                 tier="Kraken Pro Tier 1", verification="official_blog", source=KRAKEN_BLOG,
                 note="cross-platform fee tiers effective 9 July 2026"),
        FeeEntry(exchange="coinbase", effective_from=_D, maker=Decimal("0.0060"), taker=Decimal("0.0120"),
                 tier="Coinbase Advanced <$1k (conservative)", verification="secondary_sources", source=COINBASE_HELP,
                 note="sources conflict between 0.60/1.20 (<$1k) and 0.40/0.60 ($0-10k); a £500 account starts <$1k; "
                      "16 Sep 2026 'simplified, lower' schedule not published"),
        FeeEntry(exchange="bitstamp", effective_from=_D, maker=Decimal("0.0030"), taker=Decimal("0.0040"),
                 tier="Bitstamp <$10k", verification="secondary_sources", source=BITSTAMP_REVIEW,
                 note="one undated source claims 0% under $1k 30-day volume (unverified, see 'low')"),
    ])
    low = FeeSchedule(name="low", entries=[
        base.entries[0], base.entries[1],
        base.entries[2].model_copy(update={"maker": Decimal("0.0040"), "taker": Decimal("0.0060"),
                                           "tier": "Coinbase Advanced $0-10k", "note": "lower of the conflicting figures"}),
        base.entries[3].model_copy(update={"maker": Decimal("0"), "taker": Decimal("0"), "verification": "unverified",
                                           "tier": "Bitstamp <$1k (claimed)", "note": "unverified 0% claim"}),
    ])
    high = base.scaled(Decimal("1.25"), "high")
    return {"base": base, "low": low, "high": high}


# ============================================================================ returns
@dataclass(slots=True)
class ValuePoint:
    ts: datetime
    value: Decimal  # portfolio value AFTER any flow at this timestamp
    flow: Decimal = ZERO  # external deposit (+) / withdrawal (-) applied at this timestamp


def time_weighted_return(points: list[ValuePoint]) -> Decimal | None:
    """Chain-linked return that removes the effect of deposits. Sub-period i runs from the value just after
    flow i-1 to the value just before flow i (value - flow)."""
    if len(points) < 2:
        return None
    growth = Decimal(1)
    prev = points[0].value
    for p in points[1:]:
        before_flow = p.value - p.flow
        if prev > 0:
            growth *= before_flow / prev
        prev = p.value
    return growth - 1


def xirr(flows: list[tuple[datetime, Decimal]], lo: float = -0.9999, hi: float = 10.0) -> float | None:
    """Money-weighted annual return. ``flows``: investor perspective (deposits negative, final value positive).
    Bisection on NPV; returns None when no sign change exists."""
    if len(flows) < 2 or not any(a < 0 for _t, a in flows) or not any(a > 0 for _t, a in flows):
        return None
    t0 = min(t for t, _a in flows)
    cf = [((t - t0).total_seconds() / (365.0 * 86400), float(a)) for t, a in flows]

    def npv(r: float) -> float:
        return sum(a / (1 + r) ** y for y, a in cf)

    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2
