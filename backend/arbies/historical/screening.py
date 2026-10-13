"""Cross-exchange candle screening over the full historical period.

For each minute in which BOTH exchanges traded a pair (volume > 0 in both 1-minute candles), compare the two
closes against the dated round-trip retail taker fees. This is an *indicator* of whether price differences
large enough to pay fees existed - it is NOT a backtest and produces no P&L:

* a candle close is the last trade in the minute, not an executable bid/ask, and the two closes can be up to
  59 s apart;
* candles carry no order-book depth, so the size that could have been traded is unknown;
* minutes without trades on either exchange are skipped, never filled.

A minute whose close gap does not exceed fees offers no evidence of an opportunity. A minute whose gap does
exceed fees is only a candidate that genuine order-book data would have to confirm.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from arbies.db.models import ReferencePrice
from arbies.db.session import Database
from arbies.historical.plan import FeeSchedule

CAVEAT = ("Candle-close screening: last-trade prices up to 59 s apart, no depth, no execution. An indicator of "
          "whether fee-beating price gaps existed - not executable opportunities and not profit.")


async def _series(db: Database, exchange: str, symbol: str, start: datetime, end: datetime) -> dict[datetime, float]:
    async with db.session() as s:
        rows = (await s.execute(select(ReferencePrice.ts, ReferencePrice.close, ReferencePrice.volume).where(
            ReferencePrice.exchange == exchange, ReferencePrice.symbol == symbol, ReferencePrice.interval == "1m",
            ReferencePrice.ts >= start, ReferencePrice.ts <= end))).all()
    return {ts: float(c) for ts, c, v in rows if v and v > 0}


async def screen_pair(db: Database, ex_a: str, ex_b: str, symbol: str, start: datetime, end: datetime,
                      schedules: dict[str, FeeSchedule]) -> dict | None:
    a = await _series(db, ex_a, symbol, start, end)
    b = await _series(db, ex_b, symbol, start, end)
    common = sorted(set(a) & set(b))
    if not common:
        return None
    gaps = []
    per_month: dict[str, dict] = defaultdict(lambda: {"both_traded_minutes": 0, **{f"above_fees_{n}": 0 for n in schedules},
                                                       "max_abs_gap_pct": 0.0})
    above = {n: 0 for n in schedules}
    excess = {n: 0.0 for n in schedules}  # sum of (|gap| - fees) over fee-beating minutes, in % points
    top: list[tuple[float, datetime, float, float]] = []
    fee_cache: dict[tuple[str, str], float] = {}
    for ts in common:
        g = (b[ts] - a[ts]) / a[ts] * 100
        ag = abs(g)
        gaps.append(ag)
        mk = f"{ts.year}-{ts.month:02d}"
        pm = per_month[mk]
        pm["both_traded_minutes"] += 1
        pm["max_abs_gap_pct"] = max(pm["max_abs_gap_pct"], ag)
        for n, sch in schedules.items():
            key = (n, ts.date().isoformat())
            if key not in fee_cache:
                fa, fb = sch.at(ex_a, ts), sch.at(ex_b, ts)
                fee_cache[key] = float(((fa.taker if fa else Decimal("0.004")) + (fb.taker if fb else Decimal("0.004"))) * 100)
            rt = fee_cache[key]
            if ag > rt:
                above[n] += 1
                excess[n] += ag - rt
                pm[f"above_fees_{n}"] += 1
        if len(top) < 10 or ag > top[-1][0]:
            top.append((ag, ts, a[ts], b[ts]))
            top.sort(key=lambda x: -x[0])
            del top[10:]
    gaps.sort()

    def pct(p: float) -> float:
        return round(gaps[min(len(gaps) - 1, int(p / 100 * len(gaps)))], 5)

    first_fee = {n: fee_cache.get((n, common[0].date().isoformat())) for n in schedules}
    last_fee = {n: fee_cache.get((n, common[-1].date().isoformat())) for n in schedules}
    return {
        "pair": f"{ex_a} vs {ex_b} {symbol}", "exchanges": [ex_a, ex_b], "symbol": symbol,
        "minutes_both_traded": len(common), "minutes_only_one_traded": len(set(a) ^ set(b)),
        "abs_gap_pct_percentiles": {"p50": pct(50), "p90": pct(90), "p99": pct(99), "p99_9": pct(99.9),
                                    "max": round(gaps[-1], 5)},
        "round_trip_taker_fee_pct": {"first": first_fee, "last": last_fee},
        "minutes_gap_above_fees": above,
        "share_of_minutes_above_fees_pct": {n: round(100 * v / len(common), 5) for n, v in above.items()},
        "sum_excess_over_fees_pct_points": {n: round(v, 5) for n, v in excess.items()},
        "largest_gaps": [{"ts": t.isoformat(), "abs_gap_pct": round(g, 5), ex_a: pa, ex_b: pb} for g, t, pa, pb in top],
        "monthly": [{"month": k, **{kk: (round(vv, 5) if isinstance(vv, float) else vv) for kk, vv in v.items()}}
                    for k, v in sorted(per_month.items())],
    }


async def run_screening(db: Database, exchange_pairs: list[tuple[str, str]], symbols: list[str], start: datetime,
                        end: datetime, schedules: dict[str, FeeSchedule]) -> dict:
    results = []
    missing = []
    for ex_a, ex_b in exchange_pairs:
        for sym in symbols:
            r = await screen_pair(db, ex_a, ex_b, sym, start, end, schedules)
            if r is None:
                missing.append(f"{ex_a}/{ex_b} {sym}")
            else:
                results.append(r)
    return {"caveat": CAVEAT, "pairs": results, "missing": missing,
            "fee_schedules": list(schedules), "data": "1-minute exchange OHLC candles (reference_prices, interval 1m)"}
