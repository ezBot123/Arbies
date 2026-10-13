"""Orchestrates historical investment simulations: loads reference marks and genuine order-book events,
replays every variant against the same contribution schedule, computes benchmarks, the parameter study and
candle screening, and persists results with a reproducibility hash."""

from __future__ import annotations

import asyncio
import csv
import gzip
import hashlib
import io
import json
import multiprocessing
import pickle
import shutil
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete, insert, select

from arbies.core.models import BookLevel, DataSource, OrderBook, utcnow
from arbies.db.models import HistoricalSimTrade, HistoricalSimulation, OrderBookSnapshot
from arbies.db.session import Database
from arbies.historical.coverage import coverage_report, minute_sets
from arbies.historical.reference import DailyMarks, load_series
from arbies.historical.screening import run_screening
from arbies.historical.simulator import (
    PRESETS,
    STRATEGY_LABELS,
    STRATEGY_SETS,
    VALUATION_SYMBOLS,
    SimulationConfig,
    VariantReplay,
    VariantResult,
    btc_benchmark,
    cash_benchmark,
    preset_models,
)
from arbies.logging_setup import get_logger

log = get_logger(__name__)

QUESTION = ("If I had started Arbies on {start} with £{initial} and added £{monthly} every month, how much money "
            "would I have on {end}?")


class MissingReferenceData(Exception):
    pass


class HistoricalService:
    def __init__(self, db: Database, rules_for, code_revision: str = "unknown", workers: int = 1,
                 cache_dir: str = "data/historical/replay_cache"):
        self.db = db
        self.workers = max(1, workers)  # >1: variants replay in parallel worker processes (identical results)
        self.cache_dir = Path(cache_dir)
        self.rules_for = rules_for  # async (exchanges, symbols) -> (rules, notes)
        self.code_revision = code_revision
        self.progress: dict[int, float] = {}
        self.coverage_cache: dict = {}  # API-level short-lived cache of coverage reports
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ lifecycle
    async def create(self, cfg: SimulationConfig, requested_by: str = "user") -> int:
        async with self.db.session() as s:
            row = HistoricalSimulation(name=cfg.name, status="queued", config=cfg.model_dump(mode="json"),
                                       code_revision=self.code_revision, requested_by=requested_by, result={})
            s.add(row)
            await s.flush()
            return row.id

    async def submit(self, cfg: SimulationConfig, requested_by: str = "user") -> int:
        sid = await self.create(cfg, requested_by)
        t = asyncio.create_task(self.execute(sid, cfg))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return sid

    async def run_and_wait(self, cfg: SimulationConfig, requested_by: str = "user") -> dict:
        sid = await self.create(cfg, requested_by)
        await self.execute(sid, cfg)
        return await self.get(sid)

    async def marks(self, cfg: SimulationConfig) -> tuple[DailyMarks, list[str]]:
        series, notes = {}, []
        for sym in VALUATION_SYMBOLS:
            for ex in [cfg.reference_exchange] + cfg.reference_fallbacks:
                rows = await load_series(self.db, ex, sym, "1d", cfg.start - timedelta(days=10), cfg.end)
                if rows:
                    # plain values (picklable for worker processes, detached from the DB session)
                    series[sym] = [SimpleNamespace(ts=r.ts, open=r.open, close=r.close) for r in rows]
                    if ex != cfg.reference_exchange:
                        notes.append(f"reference {sym} from {ex} (no {cfg.reference_exchange} candles)")
                    break
        if "BTC/GBP" not in series or "GBP/USD" not in series:
            raise MissingReferenceData(
                "Daily reference prices (BTC/GBP and GBP/USD at least) are missing for this period. Fetch them first: "
                "python -m arbies.tools.fetch_reference --interval 1d --start <date> --end <date> "
                "--markets kraken:BTC/GBP kraken:GBP/USD kraken:ETH/GBP kraken:BTC/USD kraken:ETH/USD kraken:ETH/BTC")
        first = min(r.ts for r in series["BTC/GBP"])
        if first > cfg.start:
            notes.append(f"reference prices start {first.date()}, after the simulation start")
        return DailyMarks(series), notes

    def variant_specs(self, cfg: SimulationConfig) -> list[tuple[str, list[str], str, str]]:
        specs = [(f"{s}|{cfg.preset}|{cfg.fee_schedule}", STRATEGY_SETS[s], cfg.preset, cfg.fee_schedule)
                 for s in cfg.strategies]
        if cfg.run_parameter_study:
            base = "combined"
            for p in PRESETS:
                if p != cfg.preset:
                    specs.append((f"{base}|{p}|{cfg.fee_schedule}", STRATEGY_SETS[base], p, cfg.fee_schedule))
            for f in ("low", "base", "high"):
                if f != cfg.fee_schedule and cfg.fee_schedule != "custom":
                    specs.append((f"{base}|{cfg.preset}|{f}", STRATEGY_SETS[base], cfg.preset, f))
        seen, out = set(), []
        for sp in specs:
            if sp[0] not in seen:
                seen.add(sp[0])
                out.append(sp)
        return out

    async def execute(self, sid: int, cfg: SimulationConfig, only: list[str] | None = None) -> None:
        """Run (or resume) a simulation. ``only`` limits this invocation to some variants; finished variants are
        kept on disk, so a later call completes the rest and assembles the result."""
        self.progress[sid] = 0.0
        async with self.db.session() as s:
            row = await s.get(HistoricalSimulation, sid)
            row.status, row.started_at, row.error = "running", row.started_at or utcnow(), ""
        try:
            out = await self._run(sid, cfg, only)
            if out is None:
                async with self.db.session() as s:
                    row = await s.get(HistoricalSimulation, sid)
                    row.status = "partial"
                    row.error = "Some variants are still to run; resume this simulation to finish it."
                return
            result, trades = out
            async with self.db.session() as s:
                row = await s.get(HistoricalSimulation, sid)
                row.status, row.progress, row.finished_at = "completed", 1.0, utcnow()
                row.result = result
                row.result_hash = result["result_hash"]
                await s.execute(delete(HistoricalSimTrade).where(HistoricalSimTrade.simulation_id == sid))
                if trades:
                    for i in range(0, len(trades), 2000):
                        await s.execute(insert(HistoricalSimTrade), trades[i:i + 2000])
            shutil.rmtree(self.cache_dir / f"sim-{sid}", ignore_errors=True)  # only once results are stored
        except Exception as e:  # noqa: BLE001
            log.warning("historical simulation failed", sid=sid, error=str(e))
            async with self.db.session() as s:
                row = await s.get(HistoricalSimulation, sid)
                row.status, row.error, row.finished_at = "failed", f"{type(e).__name__}: {e}"[:4000], utcnow()
        finally:
            self.progress.pop(sid, None)

    async def _run(self, sid: int, cfg: SimulationConfig, only: list[str] | None = None) -> tuple[dict, list[dict]] | None:
        marks, notes = await self.marks(cfg)
        rules, rule_notes = await self.rules_for(cfg.exchanges, cfg.symbols)
        cov = await coverage_report(self.db, cfg.start, cfg.end, cfg.exchanges, cfg.symbols)
        sources = tuple(s.value for s in cfg.data_sources)
        sets = await minute_sets(self.db, cfg.start, cfg.end, cfg.exchanges, cfg.symbols, sources)
        days = sorted({m.date() for mins in sets.values() for m in mins})
        specs = self.variant_specs(cfg)
        meta = [{"name": name, "strategy_set": name.split("|")[0], "preset": preset, "fee_schedule": fee_name}
                for name, _st, preset, fee_name in specs]
        cfg_json = cfg.model_dump(mode="json")
        events = 0
        cache = self.cache_dir / f"sim-{sid}"
        vdir = cache / "variants"
        vdir.mkdir(parents=True, exist_ok=True)
        day_files: list[str] = []
        # phase 1: stream each day's genuine order books once (chronological across sources) into compressed frames
        # of primitive rows. Memory stays bounded by the frame size; files are kept until the simulation completes so
        # an interrupted run resumes without re-exporting.
        for i, day in enumerate(days):
            d0 = datetime(day.year, day.month, day.day, tzinfo=UTC)
            lo, hi = max(d0, cfg.start), min(d0 + timedelta(days=1) - timedelta(microseconds=1), cfg.end)
            f = cache / f"{day.isoformat()}.pkl.gz"
            cnt = cache / f"{day.isoformat()}.count"
            if not (f.exists() and cnt.exists()):
                tmp = cache / f"{day.isoformat()}.tmp"
                n = await self._write_day(tmp, sources, cfg.exchanges, cfg.symbols, lo, hi)
                tmp.replace(f)
                cnt.write_text(str(n))
            events += int(cnt.read_text())
            day_files.append(str(f))
            self.progress[sid] = 0.25 * (i + 1) / max(1, len(days))

        # phase 2: each worker parses each day once and replays its group of variants in lockstep (variants are
        # independent and deterministic, so grouping and resuming do not change any result)
        def vfile(name: str) -> Path:
            return vdir / (hashlib.sha256(name.encode()).hexdigest()[:24] + ".pkl")
        todo = [sp for sp in specs if not vfile(sp[0]).exists() and (only is None or sp[0] in only)]
        if todo:
            groups = balance_groups(todo, self.workers)
            jobs = [(g, cfg_json, rules, marks, day_files) for g in groups]

            def save(results: list[VariantResult]) -> None:
                for r in results:
                    tmp = vfile(r.name).with_suffix(".tmp")
                    with open(tmp, "wb") as fh:
                        pickle.dump(r, fh, protocol=pickle.HIGHEST_PROTOCOL)
                    tmp.replace(vfile(r.name))
            if len(jobs) > 1:
                loop = asyncio.get_running_loop()
                ctx = multiprocessing.get_context("spawn")
                with ProcessPoolExecutor(max_workers=len(jobs), mp_context=ctx) as pool:
                    futs = [loop.run_in_executor(pool, replay_group, *j) for j in jobs]
                    done = 0
                    for fut in asyncio.as_completed(futs):
                        save(await fut)
                        done += 1
                        self.progress[sid] = 0.25 + 0.65 * done / len(jobs)
            else:
                save(await asyncio.to_thread(replay_group, *jobs[0]))
        if any(not vfile(sp[0]).exists() for sp in specs):
            return None
        results: list[VariantResult] = []
        for sp in specs:
            with open(vfile(sp[0]), "rb") as fh:
                results.append(pickle.load(fh))  # written above by this service

        # ---- benchmarks (same contribution dates)
        base_fees = cfg.fees()
        cash = cash_benchmark(cfg)
        btc = btc_benchmark(cfg, marks, base_fees)

        # ---- parameter study: select on the training window only, report the unseen test window
        observed = sorted({m for mins in sets.values() for m in mins})
        split = observed[int(len(observed) * cfg.train_fraction)] if len(observed) >= 10 else None
        study = None
        if cfg.run_parameter_study:
            rows = []
            for m, r in zip(meta, results, strict=True):
                if m["strategy_set"] != "combined":
                    continue
                tr = [j for j in r.jobs if split is not None and j.created < split]
                te = [j for j in r.jobs if split is not None and j.created >= split]
                rows.append({**m, "train_trades": len(tr), "train_pnl_gbp": str(sum((j.pnl_gbp for j in tr), Decimal(0))),
                             "test_trades": len(te), "test_pnl_gbp": str(sum((j.pnl_gbp for j in te), Decimal(0))),
                             "net_trading_profit_gbp": r.summary["trading"]["net_trading_profit_gbp"],
                             "fees_gbp": r.summary["trading"]["fees_gbp"], "final_value_gbp": r.summary["final_value_gbp"]})
            presets = [x for x in rows if x["fee_schedule"] == cfg.fee_schedule]
            order = list(PRESETS)
            chosen = max(presets, key=lambda x: (Decimal(x["train_pnl_gbp"]), -order.index(x["preset"]))) if presets and split else None
            study = {"split_at": split.isoformat() if split else None, "train_fraction": cfg.train_fraction,
                     "selection_rule": "highest training-window trading P&L; ties go to the more conservative preset",
                     "chosen_preset": chosen["preset"] if chosen else None,
                     "chosen_test_pnl_gbp": chosen["test_pnl_gbp"] if chosen else None,
                     "rows": rows,
                     "note": ("Too little observed data for a train/test split." if split is None else
                              "Test-window figures were not used for selection. Fee-schedule rows are sensitivity "
                              "analysis, not candidates for selection.")}

        # ---- candle screening across the whole period (indicator only)
        schedules = {n: cfg.fees(n) for n in ("low", "base", "high")} if cfg.fee_schedule != "custom" else {"custom": cfg.fees()}
        screening = await run_screening(self.db, [("bitstamp", "coinbase")], ["BTC/USD", "BTC/GBP", "ETH/USD"],
                                        cfg.start, cfg.end, schedules)

        variants = {}
        for m, r in zip(meta, results, strict=True):
            variants[r.name] = {**m, "label": STRATEGY_LABELS.get(m["strategy_set"], m["strategy_set"]),
                                "summary": r.summary, "monthly": r.monthly, "equity": r.equity,
                                "breakdown": r.breakdown, "deposits": r.deposits, "warnings": r.warnings,
                                "result_hash": r.result_hash}
        primary = f"combined|{cfg.preset}|{cfg.fee_schedule}" if f"combined|{cfg.preset}|{cfg.fee_schedule}" in variants \
            else next(iter(variants))
        ps = variants[primary]["summary"]
        complete = ps["result_completeness"] == "complete"
        totals = cfg.plan.totals(cfg.start, cfg.end)
        headline = {
            "question": QUESTION.format(start=cfg.start.date(), initial=cfg.plan.initial_amount,
                                        monthly=cfg.plan.recurring_amount, end=cfg.end.date()),
            "answerable": complete,
            "status": "COMPLETE" if complete else "PARTIAL - trading observed for "
                      f"{ps['observed_trading_hours']} of {ps['period_hours']} hours ({ps['trading_coverage_pct']}%)",
            "primary_variant": primary,
            "total_personal_contributions_gbp": totals["total"],
            "contribution_count": totals["recurring_count"],
            "trading_pnl_gbp": ps["trading"]["net_trading_profit_gbp"],
            "trading_pnl_scope": "observed periods only" if not complete else "full period",
            "market_inventory_pnl_gbp": ps["market_inventory_pnl_gbp"],
            "conversion_costs_gbp": ps["conversion_costs_gbp"],
            "ending_value_gbp": ps["final_value_gbp"],
            "valuation_basis": f"{ps['final_valuation']} prices; reference = {ps['final_valuation_basis']}",
            "ending_value_meaning": (
                "Contributions + inventory revaluation at genuine daily exchange prices + trading P&L in observed "
                "periods only. It is NOT an estimate of what Arbies would have earned over the whole period."
                if not complete else "Full-period simulation result."),
        }
        result = {
            "headline": headline,
            "contributions": {**totals, "schedule": [{"ts": t.isoformat(), "amount_gbp": str(a), "label": lbl}
                                                     for t, a, lbl in cfg.plan.schedule(cfg.start, cfg.end)]},
            "coverage": {k: cov[k] for k in ("period", "markets", "overlap_minutes", "overlap_pct", "overlap_by_symbol",
                                             "months", "continuous", "definition")},
            "variants": variants,
            "benchmarks": {"cash": cash, "btc": btc},
            "parameter_study": study,
            "screening": screening,
            "fees": {n: cfg.fees(n).model_dump(mode="json") for n in (["custom"] if cfg.fee_schedule == "custom"
                                                                        else ["low", "base", "high"])},
            "events_replayed": events, "days_with_order_book_data": [d.isoformat() for d in days],
            "warnings": list(dict.fromkeys(notes + rule_notes + [w for r in results for w in r.warnings])),
            "assumptions": assumptions(cfg),
            "code_revision": self.code_revision,
        }
        result["result_hash"] = hashlib.sha256(json.dumps(
            {"variants": {k: v["result_hash"] for k, v in variants.items()}, "btc": btc.get("final_value_gbp"),
             "cfg": cfg.model_dump(mode="json")}, sort_keys=True, default=str).encode()).hexdigest()
        trades = [{"simulation_id": sid, "variant": r.name, "ts": j.created, "kind": j.opp.kind.value,
                   "status": j.status.value, "route": j.opp.route, "exchanges": j.opp.exchanges, "pnl_gbp": j.pnl_gbp,
                   "fees_gbp": j.fees_gbp, "hedge_loss_gbp": j.hedge_loss_gbp, "notional_gbp": j.opp.notional_gbp,
                   "detail": j.to_dict()} for r in results for j in r.jobs]
        return result, trades

    async def _write_day(self, path: Path, sources: tuple[str, ...], exchanges: list[str], symbols: list[str],
                         lo: datetime, hi: datetime, frame: int = 20000) -> int:
        S = OrderBookSnapshot
        q = (select(S.exchange, S.symbol, S.bids, S.asks, S.received_ts, S.exchange_ts, S.sequence, S.source)
             .where(S.source.in_(sources), S.exchange.in_(exchanges), S.symbol.in_(symbols), S.received_ts >= lo,
                    S.received_ts <= hi)
             .order_by(S.received_ts, S.id).execution_options(yield_per=frame))
        n = 0
        with gzip.open(path, "wb", compresslevel=1) as fh:
            async with self.db.session() as s:
                res = await s.stream(q)
                async for part in res.partitions():
                    rows = [tuple(r) for r in part]
                    n += len(rows)
                    await asyncio.to_thread(pickle.dump, rows, fh, pickle.HIGHEST_PROTOCOL)
        return n

    # ------------------------------------------------------------------ queries
    async def list(self, limit: int = 50) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(HistoricalSimulation).order_by(HistoricalSimulation.id.desc())
                                    .limit(limit))).scalars().all()
        return [self._summary(r) for r in rows]

    def _summary(self, r: HistoricalSimulation) -> dict:
        return {"id": r.id, "name": r.name, "status": r.status, "progress": self.progress.get(r.id, r.progress),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None, "error": r.error,
                "result_hash": r.result_hash, "code_revision": r.code_revision,
                "headline": (r.result or {}).get("headline")}

    async def get(self, sid: int) -> dict | None:
        async with self.db.session() as s:
            r = await s.get(HistoricalSimulation, sid)
            if r is None:
                return None
            return {**self._summary(r), "config": r.config, "result": r.result}

    async def trades(self, sid: int, variant: str | None = None, limit: int = 5000) -> list[dict]:
        q = select(HistoricalSimTrade).where(HistoricalSimTrade.simulation_id == sid)
        if variant:
            q = q.where(HistoricalSimTrade.variant == variant)
        async with self.db.session() as s:
            rows = (await s.execute(q.order_by(HistoricalSimTrade.ts).limit(limit))).scalars().all()
        return [{"id": t.id, "variant": t.variant, "ts": t.ts.isoformat(), "kind": t.kind, "status": t.status,
                 "route": t.route, "exchanges": t.exchanges, "pnl_gbp": str(t.pnl_gbp), "fees_gbp": str(t.fees_gbp),
                 "hedge_loss_gbp": str(t.hedge_loss_gbp),
                 "notional_gbp": str(t.notional_gbp) if t.notional_gbp is not None else None} for t in rows]

    async def delete(self, sid: int) -> bool:
        async with self.db.session() as s:
            await s.execute(delete(HistoricalSimTrade).where(HistoricalSimTrade.simulation_id == sid))
            res = await s.execute(delete(HistoricalSimulation).where(HistoricalSimulation.id == sid))
            return bool(res.rowcount)

    async def csv(self, sid: int, kind: str, variant: str | None = None) -> str:
        d = await self.get(sid)
        if d is None or not d["result"]:
            raise KeyError("simulation not found or not completed")
        res = d["result"]
        buf = io.StringIO()
        w = csv.writer(buf)
        v = variant or res["headline"]["primary_variant"]
        if kind == "monthly":
            rows = res["variants"][v]["monthly"]
            cols = list(rows[0].keys()) if rows else ["month"]
            w.writerow(["variant"] + cols)
            for r in rows:
                w.writerow([v] + [("" if r[c] is None else r[c]) for c in cols])
        elif kind == "equity":
            w.writerow(["variant", "ts", "value_gbp", "contributions_gbp", "net_gain_gbp", "valuation", "estimated"])
            for r in res["variants"][v]["equity"]:
                w.writerow([v, r["ts"], r["value_gbp"], r["contributions_gbp"], r["net_gain_gbp"], r["valuation"],
                            r["estimated"]])
        elif kind == "trades":
            w.writerow(["variant", "ts", "kind", "status", "route", "exchanges", "pnl_gbp", "fees_gbp",
                        "hedge_loss_gbp", "notional_gbp"])
            for t in await self.trades(sid, variant):
                w.writerow([t["variant"], t["ts"], t["kind"], t["status"], "->".join(t["route"]),
                            "/".join(t["exchanges"]), t["pnl_gbp"], t["fees_gbp"], t["hedge_loss_gbp"], t["notional_gbp"]])
        elif kind == "summary":
            w.writerow(["variant", "label", "preset", "fee_schedule", "total_contributions_gbp", "final_value_gbp",
                        "net_trading_profit_gbp", "market_inventory_pnl_gbp", "conversion_costs_gbp", "fees_gbp",
                        "trades", "trading_coverage_pct", "result_completeness"])
            for name, x in res["variants"].items():
                s = x["summary"]
                w.writerow([name, x["label"], x["preset"], x["fee_schedule"], s["total_contributions_gbp"],
                            s["final_value_gbp"], s["trading"]["net_trading_profit_gbp"], s["market_inventory_pnl_gbp"],
                            s["conversion_costs_gbp"], s["trading"]["fees_gbp"], s["trading"]["trades"],
                            s["trading_coverage_pct"], s["result_completeness"]])
            for k in ("cash", "btc"):
                b = res["benchmarks"][k]
                w.writerow([f"benchmark:{k}", b["label"], "", "", b["total_contributions_gbp"], b["final_value_gbp"],
                            "", "", "", b.get("fees_gbp", "0"), "", "", "complete"])
        else:
            raise ValueError("kind must be monthly, equity, trades or summary")
        return buf.getvalue()


WEIGHTS = {"cross_exchange": 0.6, "triangular": 0.6, "combined": 1.0}


def balance_groups(specs: list[tuple], workers: int) -> list[list[tuple]]:
    """Deterministically spread variants over ``workers`` groups by estimated cost (greedy, heaviest first)."""
    n = max(1, min(workers, len(specs)))
    groups: list[list[tuple]] = [[] for _ in range(n)]
    load = [0.0] * n
    for spec in sorted(specs, key=lambda sp: (-WEIGHTS.get(sp[0].split("|")[0], 1.0), sp[0])):
        i = load.index(min(load))
        groups[i].append(spec)
        load[i] += WEIGHTS.get(spec[0].split("|")[0], 1.0)
    return [g for g in groups if g]


_SOURCES = {v.value: v for v in DataSource}


def fast_book(ex, sym, bids, asks, rt, et, seq, src) -> OrderBook:
    """Same result as ``OrderBook.from_levels`` for stored rows (verified by test), about 1.5x faster."""
    b = [lv for lv in (BookLevel(Decimal(p), Decimal(a)) for p, a, *_ in bids) if lv.amount > 0]
    k = [lv for lv in (BookLevel(Decimal(p), Decimal(a)) for p, a, *_ in asks) if lv.amount > 0]
    if len(b) > 1:
        b.sort(key=lambda lv: lv.price, reverse=True)
    if len(k) > 1:
        k.sort(key=lambda lv: lv.price)
    return OrderBook(ex, sym, b, k, rt, et, _SOURCES[src], seq)


def iter_day(path: str):
    """Yield OrderBooks from a day file written by ``HistoricalService._write_day`` (this service's own DB rows)."""
    with gzip.open(path, "rb") as fh:
        while True:
            try:
                rows = pickle.load(fh)
            except EOFError:
                return
            for ex, sym, bids, asks, rt, et, seq, src in rows:
                yield fast_book(ex, sym, bids, asks, rt, et, seq, src)


def replay_group(specs: list[tuple], cfg_json: dict, rules: dict, marks: DailyMarks,
                 day_files: list[str]) -> list[VariantResult]:
    """Replay a group of variants over the cached days, parsing each book once. Runs in a worker process."""
    cfg = SimulationConfig.model_validate(cfg_json)
    replays = []
    for name, strategies, preset, fee_name in specs:
        params, risk, fill = preset_models(cfg, preset)
        replays.append(VariantReplay(name, strategies, cfg, params, risk, fill, cfg.fees(fee_name), rules, marks))
    for f in day_files:
        for ob in iter_day(f):
            for rp in replays:
                rp.on_book(ob)
    return [rp.finish() for rp in replays]


def assumptions(cfg: SimulationConfig) -> list[str]:
    return [
        "Trading is simulated only where genuine order-book data exists (sources: "
        + ", ".join(s.value for s in cfg.data_sources) + "). Elsewhere the bot is idle and earns nothing.",
        "Inventory is valued at fresh order-book mids when available, otherwise at the most recent completed daily "
        f"candle close on {cfg.reference_exchange} (fallbacks: {', '.join(cfg.reference_fallbacks)}).",
        f"Each deposit is split evenly across {', '.join(cfg.exchanges)}; {cfg.crypto_inventory_pct}% is converted "
        f"into crypto inventory and the rest into the fiat quote currencies, at a {cfg.plan.conversion_cost_bps} bps "
        "conversion cost (fee + spread). Conversions use reference prices and are labelled as such.",
        f"Fees follow the dated '{cfg.fee_schedule}' retail schedule; 'low' and 'high' are sensitivity runs. Maker "
        "fills are never assumed: the fill model is taker unless configured otherwise.",
        "Risk exposure limits scale with portfolio value (total <= 100%, per exchange/asset <= 60%, daily loss <= "
        "5%) so contributions do not trip limits sized for £500; per-trade size limits are unchanged."
        if cfg.scale_risk_with_capital else "Risk limits are fixed in GBP as configured.",
        f"Decisions are taken at most every {cfg.decision_interval_ms} ms of replayed time (imported books are "
        "sampled at 1 s per market); fills execute after the configured latency against the book current at that time.",
        "Rebalancing between exchanges uses the configured withdrawal fees and transfer delays.",
        "No interest, staking, tax, deposit/withdrawal bank fees or account-level fee discounts are modelled.",
        "Deposits are never counted as profit: trading P&L, market/inventory P&L and conversion costs are reported "
        "separately and reconcile to the change in portfolio value.",
    ]
