"""Contribution schedule, dated fee schedule and contribution-aware returns."""

from datetime import UTC, date, datetime
from decimal import Decimal as D

import pytest

from arbies.historical.plan import (
    ContributionPlan,
    FeeSchedule,
    ValuePoint,
    default_fee_schedules,
    time_weighted_return,
    xirr,
)

START = datetime(2025, 1, 1, tzinfo=UTC)
END = datetime(2026, 10, 13, 23, 59, tzinfo=UTC)


def test_scenario_schedule_has_21_monthly_deposits_totalling_2600():
    plan = ContributionPlan()
    s = plan.schedule(START, END)
    assert s[0] == (START, D("500"), "initial")
    monthly = [x for x in s if x[2] == "monthly"]
    assert len(monthly) == 21
    assert monthly[0][0] == datetime(2025, 2, 1, tzinfo=UTC)
    assert monthly[-1][0] == datetime(2026, 10, 1, tzinfo=UTC)
    assert all(t.day == 1 and t.hour == 0 for t, _a, _l in monthly)
    t = plan.totals(START, END)
    assert t["recurring_count"] == 21 and D(t["recurring_total"]) == D("2100") and D(t["total"]) == D("2600")


def test_schedule_respects_first_and_last_contribution_dates():
    plan = ContributionPlan(first_contribution=date(2025, 3, 15), last_contribution=date(2025, 6, 1))
    dates = [t.date() for t, _a, lbl in plan.schedule(START, END) if lbl == "monthly"]
    assert dates == [date(2025, 4, 1), date(2025, 5, 1), date(2025, 6, 1)]


def test_schedule_without_recurring_and_invalid_values():
    assert len(ContributionPlan(recurring_amount=D(0)).schedule(START, END)) == 1
    with pytest.raises(ValueError):
        ContributionPlan(recurring_amount=D(-1))
    with pytest.raises(ValueError):
        ContributionPlan(reinvest_profits=False)


def test_fee_schedule_switches_on_effective_date():
    base = default_fee_schedules()["base"]
    assert base.at("kraken", date(2026, 7, 8)).taker == D("0.0040")
    assert base.at("kraken", date(2026, 7, 9)).taker == D("0.0080")
    assert base.at("coinbase", date(2025, 6, 1)).taker == D("0.0120")
    assert base.at("unknown", date(2025, 6, 1)) is None
    assert FeeSchedule().at("kraken", date(2025, 1, 1)) is None
    assert base.change_dates(date(2025, 1, 1), date(2026, 10, 13)) == [date(2026, 7, 9)]
    high = default_fee_schedules()["high"]
    assert high.at("bitstamp", date(2025, 2, 1)).taker == D("0.0050")


def test_twr_removes_deposit_effect():
    # 100 -> grows 10% -> deposit 100 -> grows 10%: TWR = 21% regardless of the deposit
    pts = [ValuePoint(datetime(2025, 1, 1, tzinfo=UTC), D(100)),
           ValuePoint(datetime(2025, 2, 1, tzinfo=UTC), D(210), D(100)),
           ValuePoint(datetime(2025, 3, 1, tzinfo=UTC), D(231))]
    assert time_weighted_return(pts) == pytest.approx(D("0.21"))
    # deposits alone are not a return
    pts = [ValuePoint(datetime(2025, 1, 1, tzinfo=UTC), D(500)),
           ValuePoint(datetime(2025, 2, 1, tzinfo=UTC), D(600), D(100)),
           ValuePoint(datetime(2025, 3, 1, tzinfo=UTC), D(700), D(100))]
    assert time_weighted_return(pts) == 0


def test_xirr():
    # 1000 in, 1100 out after exactly one year -> 10%
    r = xirr([(datetime(2025, 1, 1, tzinfo=UTC), D(-1000)), (datetime(2026, 1, 1, tzinfo=UTC), D(1100))])
    assert r == pytest.approx(0.10, abs=1e-6)
    # cash only: deposits returned exactly -> 0%
    flows = [(datetime(2025, m, 1, tzinfo=UTC), D(-100)) for m in range(1, 13)] + [(datetime(2026, 1, 1, tzinfo=UTC), D(1200))]
    assert xirr(flows) == pytest.approx(0.0, abs=1e-6)
    assert xirr([(datetime(2025, 1, 1, tzinfo=UTC), D(-1))]) is None
