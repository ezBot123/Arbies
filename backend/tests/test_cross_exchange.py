from decimal import Decimal as D

from arbies.arbitrage.cross_exchange import CrossExchangeDetector
from arbies.arbitrage.models import RejectionReason, StrategyParams
from tests.fixtures import T0, book, rules, ts, valuator


def make(params=None, fee="0.001"):
    r = {("kraken", "BTC/USD"): rules("kraken", "BTC/USD", fee), ("coinbase", "BTC/USD"): rules("coinbase", "BTC/USD", fee)}
    return CrossExchangeDetector(params or StrategyParams(), r, valuator())


def books(ask_k="50000", bid_c="50500", amt="1", t=T0):
    return {
        ("kraken", "BTC/USD"): book("kraken", "BTC/USD", [["49990", amt]], [[ask_k, amt]], ts=t),
        ("coinbase", "BTC/USD"): book("coinbase", "BTC/USD", [[bid_c, amt]], [["50510", amt]], ts=t),
    }


def test_profitable_opportunity_net_of_all_costs():
    det = make()
    [opp] = det.evaluate(books(), T0)
    assert opp.buy_exchange == "kraken" and opp.sell_exchange == "coinbase"
    assert opp.size_base == D("0.000625")  # £25 -> $31.25 at 0.8 GBP/USD
    assert opp.notional == D("31.25")
    assert opp.gross_profit == D("0.3125")
    assert opp.fees == D("0.0628125")
    assert opp.net_profit == D("0.17134375")
    assert round(opp.net_return_pct, 4) == D("0.5483")
    assert opp.qualified, opp.rejection_reasons
    assert opp.net_profit < opp.gross_profit  # never net == gross
    assert opp.net_profit_gbp == opp.net_profit * D("0.8")


def test_fees_exceed_profit():
    det = make(fee="0.003")
    [opp] = det.evaluate(books(bid_c="50100"), T0)  # 0.2% gross vs 0.6% fees
    assert not opp.qualified
    assert RejectionReason.FEES_EXCEED_PROFIT in opp.rejection_reasons


def test_below_threshold():
    det = make(StrategyParams(min_net_profit_pct=D("2")))
    [opp] = det.evaluate(books(), T0)
    assert opp.rejection_reasons == [RejectionReason.BELOW_THRESHOLD]


def test_no_spread():
    det = make()
    [opp] = det.evaluate(books(bid_c="49000"), T0)
    assert RejectionReason.SPREAD_NON_POSITIVE in opp.rejection_reasons
    assert opp.size_base == 0


def test_stale_data_rejected():
    det = make()
    [opp] = det.evaluate(books(t=T0), ts(10_000))
    assert RejectionReason.STALE_DATA in opp.rejection_reasons
    assert opp.confidence == 0


def test_inventory_constraint_limits_size_and_rejects_when_empty():
    det = make()
    bal = {("kraken", "USD"): D("10"), ("coinbase", "BTC"): D("1")}
    [opp] = det.evaluate(books(), T0, balances=lambda e, a: bal.get((e, a), D(0)))
    assert opp.size_base < D("0.000625")
    assert opp.notional <= D("10")
    bal_empty = {("kraken", "USD"): D("100"), ("coinbase", "BTC"): D("0")}
    [opp2] = det.evaluate(books(), T0, balances=lambda e, a: bal_empty.get((e, a), D(0)))
    assert RejectionReason.INSUFFICIENT_INVENTORY in opp2.rejection_reasons


def test_depth_walk_uses_executable_prices_not_top_of_book():
    det = make(StrategyParams(max_trade_gbp=D("100000")))
    b = {
        ("kraken", "BTC/USD"): book("kraken", "BTC/USD", [["49990", "1"]], [["50000", "0.1"], ["50400", "5"]]),
        ("coinbase", "BTC/USD"): book("coinbase", "BTC/USD", [["50500", "0.1"], ["50100", "5"]], [["50510", "1"]]),
    }
    [opp] = det.evaluate(b, T0)
    # only the first 0.1 BTC crosses; deeper levels (50400 ask vs 50100 bid) do not
    assert opp.size_base == D("0.1")
    assert opp.legs[0].vwap == D("50000") and opp.legs[1].vwap == D("50500")


def test_exchange_unhealthy():
    det = make()
    [opp] = det.evaluate(books(), T0, health=lambda e: e != "coinbase")
    assert RejectionReason.EXCHANGE_UNAVAILABLE in opp.rejection_reasons


def test_min_order_size():
    r = {("kraken", "BTC/USD"): rules("kraken", "BTC/USD", "0.001", min_amount="0.001"),
         ("coinbase", "BTC/USD"): rules("coinbase", "BTC/USD", "0.001")}
    det = CrossExchangeDetector(StrategyParams(), r, valuator())
    [opp] = det.evaluate(books(), T0)
    assert RejectionReason.BELOW_MIN_ORDER in opp.rejection_reasons


def test_quote_equivalents_apply_fx_cost():
    p = StrategyParams(quote_equivalents=[["USD", "USDC"]])
    det = CrossExchangeDetector(p, {}, valuator())
    b = {
        ("kraken", "BTC/USD"): book("kraken", "BTC/USD", [["49990", "1"]], [["50000", "1"]]),
        ("coinbase", "BTC/USDC"): book("coinbase", "BTC/USDC", [["50500", "1"]], [["50510", "1"]]),
    }
    [opp] = det.evaluate(b, T0)
    assert opp.fx_cost > 0
