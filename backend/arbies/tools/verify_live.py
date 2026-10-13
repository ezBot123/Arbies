"""Verify genuine live exchange connectivity and live-data recording.

Usage (from backend/):
    .venv/bin/python -m arbies.tools.verify_live              # exchange feeds only
    .venv/bin/python -m arbies.tools.verify_live --recording  # also check DB recording (backend must be running)

Only public endpoints are used; no API keys are read. Nothing here places
orders. The tool never falls back to synthetic data: a check either observes
real exchange responses or it fails and prints the error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta

import ccxt.async_support as ccxt_async
import ccxt.pro as ccxtpro

from arbies.config import get_settings
from arbies.core.models import utcnow

REST_LIMITS = {"kraken": 25, "coinbase": 50, "gemini": 25}
WS_LIMITS = {"kraken": 25}


@dataclass
class ExchangeReport:
    exchange: str
    markets_loaded: bool = False
    available: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    rest_books: dict[str, str] = field(default_factory=dict)  # symbol -> summary or error
    ws_updates: int = 0
    ws_symbol: str | None = None
    clock_offset_ms: float | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def rest_ok(self) -> bool:
        return any(not v.startswith("ERROR") for v in self.rest_books.values())

    @property
    def ok(self) -> bool:
        return self.markets_loaded and self.rest_ok and self.ws_updates > 0


def _summ(ob: dict) -> str:
    bids, asks = ob.get("bids") or [], ob.get("asks") or []
    if not bids or not asks:
        return "ERROR empty book"
    b, a = bids[0][0], asks[0][0]
    if b >= a:
        return f"ERROR crossed book bid={b} ask={a}"
    return f"bid={b} ask={a} spread={(a - b) / a * 100:.4f}% levels={len(bids)}/{len(asks)}"


async def check_exchange(ex_id: str, symbols: list[str], ws_seconds: float) -> ExchangeReport:
    rep = ExchangeReport(ex_id)
    rest = getattr(ccxt_async, ex_id)({"enableRateLimit": True, "timeout": 15000})
    try:
        await rest.load_markets()
        rep.markets_loaded = True
        rep.available = [s for s in symbols if s in rest.markets]
        rep.unavailable = [s for s in symbols if s not in rest.markets]
        if rest.has.get("fetchTime"):
            try:
                t0 = time.time() * 1000
                server = await rest.fetch_time()
                rep.clock_offset_ms = round(float(server) - (t0 + time.time() * 1000) / 2, 1)
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"fetch_time: {type(e).__name__}: {e}"[:200])
        for s in rep.available:
            try:
                rep.rest_books[s] = _summ(await rest.fetch_order_book(s, REST_LIMITS.get(ex_id)))
            except Exception as e:  # noqa: BLE001
                rep.rest_books[s] = f"ERROR {type(e).__name__}: {e}"[:200]
    except Exception as e:  # noqa: BLE001
        rep.errors.append(f"load_markets: {type(e).__name__}: {e}"[:300])
    finally:
        await rest.close()
    if not rep.available:
        return rep
    rep.ws_symbol = rep.available[0]
    ws = getattr(ccxtpro, ex_id)({"enableRateLimit": True, "newUpdates": False})
    try:
        deadline = time.monotonic() + ws_seconds
        while time.monotonic() < deadline:
            ob = await asyncio.wait_for(ws.watch_order_book(rep.ws_symbol, WS_LIMITS.get(ex_id)),
                                        timeout=max(1.0, deadline - time.monotonic()))
            if ob.get("bids") and ob.get("asks"):
                rep.ws_updates += 1
    except TimeoutError:
        pass
    except Exception as e:  # noqa: BLE001
        rep.errors.append(f"websocket: {type(e).__name__}: {e}"[:300])
    finally:
        await ws.close()
    return rep


async def check_recording(minutes: int) -> tuple[bool, list[str]]:
    from sqlalchemy import func, select

    from arbies.db.models import OrderBookSnapshot as S
    from arbies.db.session import Database
    db = Database(get_settings().database_url)
    since = utcnow() - timedelta(minutes=minutes)
    lines = []
    try:
        async with db.session() as s:
            rows = (await s.execute(
                select(S.source, S.exchange, S.symbol, func.count(), func.max(S.received_ts))
                .where(S.received_ts >= since).group_by(S.source, S.exchange, S.symbol)
                .order_by(S.exchange, S.symbol))).all()
    finally:
        await db.dispose()
    live = [r for r in rows if r[0] == "live_recorded"]
    for src, ex, sym, n, last in rows:
        lines.append(f"  {src:<15} {ex:<10} {sym:<10} {n:>6} snapshots, last {last.isoformat()}")
    exchanges = {r[1] for r in live}
    return len(exchanges) >= 2, lines


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exchanges", nargs="*", help="override ARBIES_EXCHANGES")
    ap.add_argument("--symbols", nargs="*", help="override ARBIES_SYMBOLS")
    ap.add_argument("--ws-seconds", type=float, default=10.0)
    ap.add_argument("--recording", action="store_true", help="also verify live_recorded rows in the database")
    ap.add_argument("--minutes", type=int, default=10, help="recording lookback window")
    args = ap.parse_args()
    s = get_settings()
    exchanges = args.exchanges or s.exchanges
    symbols = args.symbols or s.symbols
    print(f"Checking public market data for {', '.join(exchanges)} / {', '.join(symbols)} (no API keys used)\n")
    reports = await asyncio.gather(*(check_exchange(e, symbols, args.ws_seconds) for e in exchanges))
    for r in reports:
        print(f"[{'PASS' if r.ok else 'FAIL'}] {r.exchange}")
        print(f"    markets loaded: {r.markets_loaded}; available: {', '.join(r.available) or '-'}"
              + (f"; NOT listed: {', '.join(r.unavailable)}" if r.unavailable else ""))
        if r.clock_offset_ms is not None:
            print(f"    clock offset vs exchange: {r.clock_offset_ms} ms")
        for sym, summ in r.rest_books.items():
            print(f"    REST {sym:<10} {summ}")
        if r.ws_symbol:
            print(f"    WebSocket {r.ws_symbol}: {r.ws_updates} order-book updates in {args.ws_seconds:.0f}s")
        for e in r.errors:
            print(f"    error: {e}")
        if any("403" in e or "Forbidden" in e or "ProxyError" in e for e in r.errors):
            print("    hint: a 403 at connect time usually means a proxy/firewall/network policy is blocking this host")
    passed = [r.exchange for r in reports if r.ok]
    print(f"\nExchange feeds: {len(passed)}/{len(reports)} verified ({', '.join(passed) or 'none'})")
    ok = len(passed) >= 2
    if args.recording:
        rec_ok, lines = await check_recording(args.minutes)
        print(f"\nRecorded snapshots in the last {args.minutes} min:")
        print("\n".join(lines) or "  none")
        print(f"[{'PASS' if rec_ok else 'FAIL'}] live_recorded data from at least 2 exchanges")
        ok = ok and rec_ok
    print("\nOVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
