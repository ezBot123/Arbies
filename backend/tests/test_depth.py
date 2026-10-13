from decimal import Decimal as D

from arbies.arbitrage.depth import depth_within, fill_by_base, fill_by_quote, max_crossing_amount
from arbies.core.models import BookLevel

ASKS = [BookLevel(D("100"), D("1")), BookLevel(D("101"), D("2")), BookLevel(D("103"), D("5"))]
BIDS = [BookLevel(D("99"), D("1")), BookLevel(D("98"), D("2"))]


def test_fill_by_base_walks_levels_and_computes_vwap():
    est = fill_by_base(ASKS, D("2"))
    assert est.fully_filled
    assert est.quote_amount == D("201")  # 100*1 + 101*1
    assert est.vwap == D("100.5")
    assert est.levels_used == 2
    assert est.worst_price == D("101")


def test_fill_by_base_partial_when_book_too_thin():
    est = fill_by_base(BIDS, D("5"), is_buy=False)
    assert not est.fully_filled
    assert est.base_amount == D("3")
    assert est.quote_amount == D("99") + D("196")


def test_fill_by_base_respects_limit_price():
    est = fill_by_base(ASKS, D("4"), limit_price=D("101"))
    assert est.base_amount == D("3")
    assert not est.fully_filled
    est_sell = fill_by_base(BIDS, D("3"), limit_price=D("99"), is_buy=False)
    assert est_sell.base_amount == D("1")


def test_fill_by_quote():
    est = fill_by_quote(ASKS, D("150"))
    assert est.fully_filled
    assert est.base_amount == D("1") + D("50") / D("101")
    assert est.quote_amount == D("150")


def test_max_crossing_amount():
    asks = [BookLevel(D("100"), D("1")), BookLevel(D("102"), D("1"))]
    bids = [BookLevel(D("103"), D("0.5")), BookLevel(D("101"), D("2"))]
    # 0.5 @ (100 vs 103), 0.5 @ (100 vs 101), then ask 102 > bid 101 stops
    assert max_crossing_amount(asks, bids) == D("1.0")
    assert max_crossing_amount([], bids) == 0


def test_depth_within():
    assert depth_within(ASKS, D("100"), D("1"), is_ask=True) == D("3")
