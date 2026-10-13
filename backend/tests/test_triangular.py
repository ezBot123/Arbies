from decimal import Decimal as D

from arbies.arbitrage.models import RejectionReason, StrategyParams
from arbies.arbitrage.triangular import TriangularDetector, discover_cycles, simulate_cycle
from arbies.core.models import Side
from tests.fixtures import T0, book, rules, valuator


def test_discover_cycles():
    cycles = discover_cycles(["BTC/USD", "ETH/USD", "ETH/BTC"], ["USD"])
    routes = {tuple([c[0].src] + [h.dst for h in c]) for c in cycles}
    assert routes == {("USD", "BTC", "ETH", "USD"), ("USD", "ETH", "BTC", "USD")}
    c = next(c for c in cycles if c[0].dst == "BTC")
    assert [h.side for h in c] == [Side.BUY, Side.BUY, Side.SELL]


def _books(eth_btc_ask="0.049"):
    # implied ETH/BTC = 2500/50000 = 0.05; buying ETH with BTC at 0.049 is cheap
    return {
        ("kraken", "BTC/USD"): book("kraken", "BTC/USD", [["49995", "10"]], [["50000", "10"]]),
        ("kraken", "ETH/USD"): book("kraken", "ETH/USD", [["2500", "100"]], [["2501", "100"]]),
        ("kraken", "ETH/BTC"): book("kraken", "ETH/BTC", [["0.0489", "100"]], [[eth_btc_ask, "100"]]),
    }


def test_simulate_cycle_math_without_fees():
    b = {s: ob for (_e, s), ob in _books().items()}
    cycles = discover_cycles(list(b), ["USD"])
    c = next(c for c in cycles if c[0].dst == "BTC")
    final, hops, consumed = simulate_cycle(c, D("1000"), b, {}, {}, apply_fees=False)
    # 1000 USD -> 0.02 BTC -> 0.02/0.049 ETH -> *2500 USD
    expected = D("0.02") / D("0.049") * D("2500")
    assert abs(final - expected) < D("1e-18")
    assert consumed == D("1000")
    assert all(h.filled for h in hops)


def test_triangular_detector_qualifies_with_fees():
    det = TriangularDetector(StrategyParams(start_currencies=["USD"]), {
        ("kraken", s): rules("kraken", s, "0.001") for s in ["BTC/USD", "ETH/USD", "ETH/BTC"]
    }, valuator())
    opps = det.evaluate(_books(), T0)
    best = max(opps, key=lambda o: o.net_return_pct)
    assert best.route == ["USD", "BTC", "ETH", "USD"]
    assert best.gross_profit > best.net_profit > 0
    assert best.fees > 0
    assert best.qualified, best.rejection_reasons
    assert best.notional <= D("31.25")


def test_triangular_no_inefficiency():
    det = TriangularDetector(StrategyParams(start_currencies=["USD"]), {}, valuator())
    opps = det.evaluate(_books(eth_btc_ask="0.0502"), T0)
    for o in opps:
        assert not o.qualified


def test_triangular_lot_size_and_min_order():
    r = {("kraken", s): rules("kraken", s, "0.001") for s in ["BTC/USD", "ETH/USD"]}
    r[("kraken", "ETH/BTC")] = rules("kraken", "ETH/BTC", "0.001", step="0.1", min_amount="1")
    det = TriangularDetector(StrategyParams(start_currencies=["USD"]), r, valuator())
    opps = det.evaluate(_books(), T0)
    best = next(o for o in opps if o.route[1] == "BTC")
    # £25 is far too small for a 1 ETH minimum
    assert RejectionReason.BELOW_MIN_ORDER in best.rejection_reasons or \
        RejectionReason.INSUFFICIENT_LIQUIDITY in best.rejection_reasons


def test_triangular_partial_liquidity_rejected():
    b = _books()
    b[("kraken", "ETH/BTC")] = book("kraken", "ETH/BTC", [["0.0489", "0.0001"]], [["0.049", "0.0001"]])
    det = TriangularDetector(StrategyParams(start_currencies=["USD"]), {}, valuator())
    best = next(o for o in det.evaluate(b, T0) if o.route[1] == "BTC")
    assert RejectionReason.INSUFFICIENT_LIQUIDITY in best.rejection_reasons
