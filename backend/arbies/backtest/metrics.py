"""Backtest analytics."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from arbies.core.models import ZERO, split_symbol
from arbies.core.valuation import Valuator
from arbies.execution.simulator import ArbitrageExecutor, ExecutionJob, JobStatus


def max_drawdown(curve: list[tuple[datetime, Decimal]]) -> tuple[Decimal, Decimal]:
    """Returns (max drawdown in GBP, max drawdown in % of peak)."""
    peak = None
    mdd = ZERO
    mdd_pct = ZERO
    for _t, v in curve:
        if peak is None or v > peak:
            peak = v
        dd = peak - v
        if dd > mdd:
            mdd = dd
        if peak and peak > 0 and dd / peak * 100 > mdd_pct:
            mdd_pct = dd / peak * 100
    return mdd, mdd_pct


def sharpe_daily(curve: list[tuple[datetime, Decimal]]) -> tuple[float | None, str]:
    """Annualised Sharpe from daily close-to-close equity returns.

    Assumptions: risk-free rate 0, 365 trading days per year (crypto trades daily),
    requires at least 3 full daily returns. Returns (value, note)."""
    if not curve:
        return None, "no equity data"
    daily: dict[str, Decimal] = {}
    for t, v in curve:
        daily[t.date().isoformat()] = v  # last value of each day
    vals = [float(v) for _d, v in sorted(daily.items())]
    rets = [(vals[i] / vals[i - 1] - 1) for i in range(1, len(vals)) if vals[i - 1] > 0]
    if len(rets) < 3:
        return None, f"insufficient history: {len(rets)} daily returns (need >= 3)"
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return None, "zero variance of daily returns"
    return mean / math.sqrt(var) * math.sqrt(365), f"{len(rets)} daily returns, rf=0, 365-day annualisation"


def compute_metrics(*, initial_value: Decimal, final_value: Decimal, curve: list[tuple[datetime, Decimal]],
                    jobs: list[ExecutionJob], counters: dict, rejection_counts: dict[str, int],
                    risk_rejections: dict[str, int], exposure_samples: list[Decimal], final_exposure: Decimal,
                    rebalance_costs_gbp: Decimal, rebalances: int, marks_for_volume: Valuator | None = None
                    ) -> dict:
    total_pnl = final_value - initial_value
    realised = sum((j.pnl_gbp for j in jobs if j.done), ZERO)
    fees = sum((j.fees_gbp for j in jobs), ZERO)
    hedge_losses = sum((j.hedge_loss_gbp for j in jobs), ZERO)
    slippage = sum((j.slippage_gbp for j in jobs if j.slippage_gbp > 0), ZERO)
    attempted = len(jobs)
    successful = sum(1 for j in jobs if j.status == JobStatus.SUCCESS)
    failed = sum(1 for j in jobs if j.status == JobStatus.FAILED)
    partial = sum(1 for j in jobs if j.status in (JobStatus.PARTIAL_HEDGED, JobStatus.PARTIAL_HELD))
    req = filled = ZERO
    volume = ZERO
    by_ex: dict[str, Decimal] = defaultdict(lambda: ZERO)
    by_pair: dict[str, dict] = defaultdict(lambda: {"pnl_gbp": ZERO, "trades": 0, "fees_gbp": ZERO})
    by_day: dict[str, dict] = defaultdict(lambda: {"pnl_gbp": ZERO, "trades": 0})
    by_hour: dict[int, dict] = defaultdict(lambda: {"pnl_gbp": ZERO, "trades": 0})
    margins = []
    for j in jobs:
        marks = j._marks
        for f in j.fills:
            if f.purpose == "entry":
                req += f.requested_base
                filled += f.filled_base
            if marks is not None and f.filled_base > 0:
                q = split_symbol(f.symbol)[1]
                volume += marks.to_reporting(q, f.quote_amount)
                # exchange attribution: value change of each fill at execution marks
                by_ex[f.exchange] += ArbitrageExecutor._fill_value_change(f, marks)
        label = " / ".join(j.opp.symbols) if j.opp.kind.value == "cross_exchange" else "->".join(j.opp.route)
        key = f"{j.opp.kind.value}: {label}"
        by_pair[key]["pnl_gbp"] += j.pnl_gbp
        by_pair[key]["trades"] += 1
        by_pair[key]["fees_gbp"] += j.fees_gbp
        d = j.created.date().isoformat()
        by_day[d]["pnl_gbp"] += j.pnl_gbp
        by_day[d]["trades"] += 1
        by_hour[j.created.hour]["pnl_gbp"] += j.pnl_gbp
        by_hour[j.created.hour]["trades"] += 1
        if j.opp.notional_gbp:
            margins.append(j.pnl_gbp / j.opp.notional_gbp * 100)
    mdd, mdd_pct = max_drawdown(curve)
    sharpe, sharpe_note = sharpe_daily(curve)

    def s(x):
        return str(x.quantize(Decimal("0.00000001"))) if isinstance(x, Decimal) else x

    return {
        "initial_value_gbp": s(initial_value),
        "final_value_gbp": s(final_value),
        "total_pnl_gbp": s(total_pnl),
        "net_return_pct": s(total_pnl / initial_value * 100 if initial_value else ZERO),
        "realised_trade_pnl_gbp": s(realised),
        "market_and_inventory_pnl_gbp": s(total_pnl - realised + rebalance_costs_gbp),
        "max_drawdown_gbp": s(mdd),
        "max_drawdown_pct": s(mdd_pct),
        "trading_volume_gbp": s(volume),
        "opportunities_detected": counters.get("detected", 0),
        "opportunities_eligible": counters.get("eligible", 0),
        "opportunity_evaluations": counters.get("evaluations", 0),
        "trades_attempted": attempted,
        "successful_cycles": successful,
        "partial_cycles": partial,
        "failed_cycles": failed,
        "fill_ratio": float(filled / req) if req > 0 else None,
        "avg_net_margin_pct": s(sum(margins, ZERO) / len(margins)) if margins else None,
        "fees_paid_gbp": s(fees),
        "slippage_losses_gbp": s(slippage),
        "failed_hedge_losses_gbp": s(hedge_losses),
        "rebalancing_costs_gbp": s(rebalance_costs_gbp),
        "rebalances": rebalances,
        "max_inventory_exposure_gbp": s(max(exposure_samples) if exposure_samples else ZERO),
        "final_inventory_exposure_gbp": s(final_exposure),
        "pnl_by_exchange": {k: s(v) for k, v in sorted(by_ex.items())},
        "pnl_by_pair": {k: {"pnl_gbp": s(v["pnl_gbp"]), "trades": v["trades"], "fees_gbp": s(v["fees_gbp"])}
                        for k, v in sorted(by_pair.items())},
        "pnl_by_day": {k: {"pnl_gbp": s(v["pnl_gbp"]), "trades": v["trades"]} for k, v in sorted(by_day.items())},
        "pnl_by_hour_utc": {str(k): {"pnl_gbp": s(v["pnl_gbp"]), "trades": v["trades"]} for k, v in sorted(by_hour.items())},
        "sharpe_ratio": sharpe,
        "sharpe_note": sharpe_note,
        "rejection_reasons": dict(sorted(rejection_counts.items())),
        "risk_rejections": dict(sorted(risk_rejections.items())),
        "notes": [
            "Trade P&L is the portfolio value change at mid-price marks frozen at decision time.",
            "market_and_inventory_pnl_gbp is the change in value of held inventory due to price moves.",
        ],
    }
