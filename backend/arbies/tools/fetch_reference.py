"""Fetch genuine exchange OHLC candles into ``reference_prices`` (resumable).

Usage (from backend/):
    .venv/bin/python -m arbies.tools.fetch_reference --interval 1d --start 2024-12-25 --end 2026-10-14 \\
        --markets kraken:BTC/GBP kraken:GBP/USD bitstamp:BTC/USD
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

from arbies.config import get_settings
from arbies.db.session import Database
from arbies.historical.reference import fetch_range


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", default="1d", choices=["1m", "1h", "1d"])
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--markets", nargs="+", required=True, help="exchange:SYMBOL ...")
    a = ap.parse_args()
    start = datetime.fromisoformat(a.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(a.end).replace(tzinfo=UTC)
    db = Database(get_settings().database_url)
    try:
        for m in a.markets:
            ex, sym = m.split(":", 1)
            t0 = datetime.now(UTC)
            try:
                r = await fetch_range(db, ex, sym, a.interval, start, end)
                print(f"{m} {a.interval}: fetched {r['fetched']} stored {r['stored']} "
                      f"({(datetime.now(UTC) - t0).total_seconds():.0f}s)", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"{m} {a.interval}: FAILED {type(e).__name__}: {e}", flush=True)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
