"""Download and import Tardis.dev free historical order-book samples (first day of each month only).

No API key is used or needed: Tardis serves the first day of every month without authentication; any other day
returns 401 and would require a paid subscription, which this tool never attempts. Each file is streamed to a
work directory, imported (provenance + SHA-256 recorded in ``historical_datasets``), then deleted. Re-running
skips files already imported, so an interrupted run can simply be restarted.

Usage (from backend/):
    .venv/bin/python -m arbies.tools.fetch_tardis --start 2025-01 --end 2026-10 --exchanges coinbase bitstamp kraken
"""

from __future__ import annotations

import argparse
import asyncio
import os
import ssl
import time
from datetime import date
from pathlib import Path

import aiohttp
from sqlalchemy import select

from arbies.config import get_settings
from arbies.db.models import HistoricalDataset
from arbies.db.session import Database
from arbies.historical.importer import HistoricalImporter

BASE = "https://datasets.tardis.dev/v1"
PAIRS = ["BTC/USD", "ETH/USD", "BTC/GBP", "ETH/GBP", "ETH/BTC"]
KRAKEN_RENAME = date(2026, 7, 10)  # Tardis lists Kraken spot as XBT-* before this date and BTC-* from it


def tardis_symbol(exchange: str, pair: str, day: date) -> str:
    b, q = pair.split("/")
    if exchange == "bitstamp":
        return f"{b}{q}"
    if exchange == "kraken" and day < KRAKEN_RENAME:
        b, q = ("XBT" if b == "BTC" else b), ("XBT" if q == "BTC" else q)
    return f"{b}-{q}"


def months(start: str, end: str) -> list[date]:
    y, m = map(int, start.split("-"))
    ye, me = map(int, end.split("-"))
    out = []
    while (y, m) <= (ye, me):
        out.append(date(y, m, 1))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


async def already_imported(db: Database, url: str) -> bool:
    async with db.session() as s:
        row = (await s.execute(select(HistoricalDataset.id).where(HistoricalDataset.source_url == url,
                                                                   HistoricalDataset.status == "completed"))).first()
    return row is not None


async def download(session: aiohttp.ClientSession, ctx: ssl.SSLContext, url: str, dest: Path) -> tuple[int, int]:
    async with session.get(url, ssl=ctx, timeout=aiohttp.ClientTimeout(total=3600, sock_read=120)) as r:
        if r.status != 200:
            return r.status, 0
        n = 0
        with open(dest, "wb") as fh:
            async for chunk in r.content.iter_chunked(1 << 20):
                fh.write(chunk)
                n += len(chunk)
        return 200, n


async def run_exchange(db: Database, exchange: str, days: list[date], data_type: str, interval_ms: int, depth: int,
                       workdir: Path) -> None:
    ctx = ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE"))
    imp = HistoricalImporter(db, batch_rows=5000)
    wd = workdir / exchange
    wd.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession(trust_env=True) as session:
        for day in days:
            for pair in PAIRS:
                sym = tardis_symbol(exchange, pair, day)
                url = f"{BASE}/{exchange}/{data_type}/{day:%Y/%m/%d}/{sym}.csv.gz"
                if await already_imported(db, url):
                    print(f"skip {url} (already imported)", flush=True)
                    continue
                dest = wd / f"{exchange}_{data_type}_{day}_{sym.replace('/', '-')}.csv.gz"
                t0 = time.time()
                status, size = 0, 0
                for attempt in range(4):
                    try:
                        status, size = await download(session, ctx, url, dest)
                        break
                    except (TimeoutError, aiohttp.ClientError) as e:
                        print(f"retry {url}: {type(e).__name__}", flush=True)
                        await asyncio.sleep(5 * (attempt + 1))
                if status != 200:
                    print(f"MISSING {url} -> HTTP {status}", flush=True)
                    dest.unlink(missing_ok=True)
                    continue
                t1 = time.time()
                try:
                    r = await imp.import_file(dest, provider="tardis.dev", source_url=url, sample_interval_ms=interval_ms,
                                              depth=depth, notes=f"free first-of-month sample; {data_type}")
                    v = r.validation
                    print(f"OK {exchange} {pair} {day} {size / 1e6:.1f}MB dl {t1 - t0:.0f}s import {time.time() - t1:.0f}s "
                          f"rows {v['rows']} snapshots {r.snapshots_written} crossed {v['crossed_emitted']} "
                          f"ooo {v['out_of_order_local']} gaps {v['gap_count']} invalid {v['invalid_rows']} [{r.status}]",
                          flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"FAILED import {url}: {type(e).__name__}: {e}", flush=True)
                finally:
                    dest.unlink(missing_ok=True)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-01")
    ap.add_argument("--end", default="2026-10")
    ap.add_argument("--exchanges", nargs="+", default=["coinbase", "bitstamp", "kraken"])
    ap.add_argument("--data-type", default="book_snapshot_5")
    ap.add_argument("--interval-ms", type=int, default=1000)
    ap.add_argument("--depth", type=int, default=5)
    ap.add_argument("--workdir", default="data/historical/tardis_tmp")
    a = ap.parse_args()
    db = Database(get_settings().database_url)
    try:
        await asyncio.gather(*(run_exchange(db, ex, months(a.start, a.end), a.data_type, a.interval_ms, a.depth,
                                            Path(a.workdir)) for ex in a.exchanges))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
