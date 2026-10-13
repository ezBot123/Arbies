"""Run or resume a historical investment simulation from the command line (no web server needed).

    .venv/bin/python -m arbies.tools.run_historical --name "..." --workers 3 --only "cross_exchange|moderate|base"
    .venv/bin/python -m arbies.tools.run_historical --resume 4 --workers 4

Finished variants are kept on disk, so long simulations can be split across several invocations; the final
invocation assembles the result. Results appear in the dashboard's Historical Simulator.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from arbies.backtest.service import BacktestService
from arbies.config import get_settings
from arbies.db.models import HistoricalSimulation
from arbies.db.session import Database
from arbies.historical.service import HistoricalService
from arbies.historical.simulator import SimulationConfig


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name")
    ap.add_argument("--resume", type=int, help="simulation id to resume")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--only", nargs="*", help="variant names to run in this invocation")
    ap.add_argument("--list-variants", action="store_true")
    a = ap.parse_args()
    st = get_settings()
    db = Database(st.database_url)
    bt = BacktestService(db, lambda: {}, st.fallback_fx_to_gbp)
    svc = HistoricalService(db, bt.rules_for, bt.code_revision, workers=a.workers)
    try:
        if a.resume:
            async with db.session() as s:
                cfg = SimulationConfig.model_validate((await s.get(HistoricalSimulation, a.resume)).config)
            sid = a.resume
        else:
            cfg = SimulationConfig(**({"name": a.name} if a.name else {}))
            if a.list_variants:
                print("\n".join(sp[0] for sp in svc.variant_specs(cfg)))
                return
            sid = await svc.create(cfg, requested_by="cli")
        print(f"simulation {sid}: starting ({'resume' if a.resume else 'new'}), variants: {a.only or 'all remaining'}",
              flush=True)
        t0 = time.time()
        task = asyncio.create_task(svc.execute(sid, cfg, only=a.only))
        last = -1
        while not task.done():
            await asyncio.sleep(30)
            p = round(svc.progress.get(sid, 1.0) * 100)
            if p != last:
                print(f"  progress {p}% ({(time.time() - t0) / 60:.1f} min)", flush=True)
                last = p
        await task
        d = await svc.get(sid)
        print(f"simulation {sid}: {d['status']} {d['error']} in {(time.time() - t0) / 60:.1f} min", flush=True)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
