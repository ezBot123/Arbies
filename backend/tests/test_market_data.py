"""CCXT adapter behaviour with a mocked exchange: normalisation, rules, errors, reconnect, REST fallback."""

import asyncio
from decimal import Decimal as D

import ccxt

from arbies.core.models import DataSource
from arbies.market_data.base import ConnStatus
from arbies.market_data.ccxt_adapter import CcxtAdapter


class FakeExchange:
    precisionMode = ccxt.TICK_SIZE

    def __init__(self, fail_ws_times=0, bad_symbol=None):
        self.has = {"watchOrderBook": True, "fetchTime": True}
        self.markets = {
            "BTC/USD": {"symbol": "BTC/USD", "base": "BTC", "quote": "USD", "taker": 0.004, "maker": 0.0025,
                        "precision": {"amount": 0.00000001, "price": 0.1},
                        "limits": {"amount": {"min": 0.0001}, "cost": {"min": 5}}, "active": True},
            "ETH/USD": {"symbol": "ETH/USD", "base": "ETH", "quote": "USD", "taker": 0.004, "maker": 0.0025,
                        "precision": {"amount": 0.0001, "price": 0.01}, "limits": {}, "active": True},
        }
        self.fail_ws_times = fail_ws_times
        self.ws_calls = 0
        self.rest_calls = 0
        self.bad_symbol = bad_symbol
        self.closed = False

    async def load_markets(self):
        return self.markets

    async def fetch_time(self):
        import time
        return int(time.time() * 1000) + 50

    async def watch_order_book(self, symbol, limit=None):
        self.ws_calls += 1
        if symbol == self.bad_symbol:
            raise ccxt.BadSymbol("nope")
        if self.ws_calls <= self.fail_ws_times:
            raise ccxt.NetworkError("socket closed")
        await asyncio.sleep(0.01)
        return {"bids": [[100.0, 1.0], [99.5, 2.0]], "asks": [[100.5, 1.5]], "timestamp": None, "nonce": self.ws_calls}

    async def fetch_order_book(self, symbol, limit=None):
        self.rest_calls += 1
        await asyncio.sleep(0.01)
        return {"bids": [[100.0, 1.0]], "asks": [[100.5, 1.5]], "timestamp": None}

    async def close(self):
        self.closed = True


_real_sleep = asyncio.sleep


async def _run(adapter, seconds):
    await adapter.start()
    await asyncio.sleep(seconds)
    await adapter.stop()


async def test_adapter_normalises_books_and_rules():
    books, events = [], []
    fake = FakeExchange()
    ad = CcxtAdapter("kraken", ["BTC/USD", "ETH/USD", "DOGE/USD"], books.append, lambda *a: events.append(a),
                     exchange_factory=lambda _id: fake)
    await _run(ad, 0.2)
    assert books
    ob = books[0]
    assert ob.source == DataSource.LIVE and ob.best_bid == D("100.0") and ob.best_ask == D("100.5")
    assert ob.bids[0].price > ob.bids[1].price
    r = ad.market_rules()["BTC/USD"]
    assert r.taker_fee == D("0.004") and r.amount_step == D("1E-8") and r.min_cost == D("5")
    assert r.fee_source == "exchange_public_schedule"
    assert ad.health.unavailable_symbols == ["DOGE/USD"]
    assert ad.health.clock_offset_ms is not None
    assert any(e[2] == "connected" for e in events)
    assert fake.closed


async def test_adapter_falls_back_to_rest_after_ws_failures(monkeypatch):
    import arbies.market_data.ccxt_adapter as mod

    async def fast_sleep(s):
        await _real_sleep(min(s, 0.01))
    monkeypatch.setattr(mod.asyncio, "sleep", fast_sleep)
    books, events = [], []
    fake = FakeExchange(fail_ws_times=10_000)
    ad = CcxtAdapter("kraken", ["BTC/USD"], books.append, lambda *a: events.append(a),
                     exchange_factory=lambda _id: fake, rest_interval_s=0.01)
    await ad.start()
    for _ in range(100):
        await _real_sleep(0.01)
        if books:
            break
    await ad.stop()
    assert any(e[2] == "fallback_rest" for e in events)
    assert fake.rest_calls > 0 and books
    assert ad.health.errors >= 3 and ad.health.reconnects >= 3


async def test_adapter_drops_unsupported_symbol():
    books, events = [], []
    fake = FakeExchange(bad_symbol="ETH/USD")
    ad = CcxtAdapter("kraken", ["BTC/USD", "ETH/USD"], books.append, lambda *a: events.append(a),
                     exchange_factory=lambda _id: fake)
    await _run(ad, 0.1)
    assert "ETH/USD" in ad.health.unavailable_symbols
    assert ad.health.status == ConnStatus.DISCONNECTED  # after stop


async def test_load_markets_failure_reports_error(monkeypatch):
    import arbies.market_data.ccxt_adapter as mod

    async def fast_sleep(s):
        await _real_sleep(0.005)
    monkeypatch.setattr(mod.asyncio, "sleep", fast_sleep)

    class Broken(FakeExchange):
        async def load_markets(self):
            raise ccxt.ExchangeNotAvailable("403 Forbidden")
    events = []
    ad = CcxtAdapter("kraken", ["BTC/USD"], lambda ob: None, lambda *a: events.append(a),
                     exchange_factory=lambda _id: Broken())
    await ad.start()
    await _real_sleep(0.05)
    assert ad.health.status == ConnStatus.ERROR
    assert "403" in ad.health.last_error
    await ad.stop()
