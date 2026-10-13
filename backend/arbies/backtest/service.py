"""Backtest orchestration: persistence, background execution, comparison and
walk-forward validation."""

from __future__ import annotations

import asyncio
import itertools
import subprocess
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, insert, select

from arbies.arbitrage.models import StrategyParams
from arbies.backtest.config import BacktestConfig
from arbies.backtest.dataset import dataset_summary, find_gaps, load_books
from arbies.backtest.engine import BacktestEngine, BacktestResult, InsufficientDataError
from arbies.core.models import DataSource, MarketRules, split_symbol, utcnow
from arbies.db.models import BacktestRun, BacktestTrade, Market, OrderBookSnapshot, WalkForwardStudy
from arbies.db.session import Database
from arbies.logging_setup import get_logger
from arbies.market_data.recorder import snapshot_row
from arbies.market_data.synthetic import generate_history

log = get_logger(__name__)


def detect_code_revision() -> str:
    import os
    if os.environ.get("ARBIES_CODE_REVISION"):
        return os.environ["ARBIES_CODE_REVISION"]
    try:
        root = Path(__file__).resolve().parents[3]
        rev = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root, capture_output=True, text=True,
                             timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        return (rev + ("-dirty" if dirty else "")) if rev else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _dec(v):
    return str(v) if v is not None else None


def run_summary(r: BacktestRun) -> dict:
    return {
        "id": r.id, "name": r.name, "status": r.status, "progress": r.progress,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "strategies": r.config.get("strategies"), "exchanges": r.config.get("exchanges"),
        "symbols": r.config.get("symbols"), "start": r.config.get("start"), "end": r.config.get("end"),
        "data_source": r.data_source, "dataset_id": r.dataset_id, "code_revision": r.code_revision,
        "seed": r.seed, "result_hash": r.result_hash, "error": r.error, "requested_by": r.requested_by,
        "strategy_name": r.strategy_name, "strategy_version": r.strategy_version,
        "walkforward_id": r.walkforward_id, "walkforward_segment": r.walkforward_segment,
        "headline": {k: r.metrics.get(k) for k in ("total_pnl_gbp", "net_return_pct", "max_drawdown_pct",
                                                    "trades_attempted", "successful_cycles", "fees_paid_gbp")}
        if r.metrics else {},
    }


class BacktestService:
    def __init__(self, db: Database, rules_provider: Callable[[], dict[tuple[str, str], MarketRules]],
                 fallback_fx: dict[str, Decimal], code_revision: str | None = None):
        self.db = db
        self.rules_provider = rules_provider
        self.fallback_fx = fallback_fx
        self.code_revision = code_revision or detect_code_revision()
        self.progress: dict[int, float] = {}
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ rules
    async def rules_for(self, exchanges: list[str], symbols: list[str]) -> tuple[dict, list[str]]:
        live = self.rules_provider() or {}
        rules: dict[tuple[str, str], MarketRules] = {}
        notes = []
        async with self.db.session() as s:
            rows = (await s.execute(select(Market).where(Market.exchange.in_(exchanges),
                                                         Market.symbol.in_(symbols)))).scalars().all()
        stored = {(m.exchange, m.symbol): m for m in rows}
        for ex in exchanges:
            for sym in symbols:
                k = (ex, sym)
                if k in live:
                    rules[k] = live[k]
                elif k in stored:
                    m = stored[k]
                    rules[k] = MarketRules(ex, sym, m.base, m.quote, taker_fee=m.taker_fee or Decimal("0.0026"),
                                           maker_fee=m.maker_fee or Decimal("0.0016"), amount_step=m.amount_step,
                                           price_step=m.price_step, min_amount=m.min_amount, min_cost=m.min_cost,
                                           fee_source=m.fee_source)
                else:
                    b, q = split_symbol(sym)
                    rules[k] = MarketRules(ex, sym, b, q, fee_source="default")
                    notes.append(f"{ex} {sym}: no recorded market rules; default 0.26% taker fee and no lot-size rules")
        return rules, notes

    # ------------------------------------------------------------------ runs
    async def create_run(self, cfg: BacktestConfig, requested_by: str = "user", strategy_name: str | None = None,
                         strategy_version: int | None = None, walkforward_id: int | None = None,
                         segment: str | None = None) -> int:
        async with self.db.session() as s:
            run = BacktestRun(name=cfg.name or f"{'+'.join(cfg.strategies)} {cfg.start:%Y-%m-%d}",
                              status="queued", config=cfg.model_dump(mode="json"), code_revision=self.code_revision,
                              data_source=cfg.data_source.value, seed=cfg.seed, requested_by=requested_by,
                              strategy_name=strategy_name, strategy_version=strategy_version,
                              walkforward_id=walkforward_id, walkforward_segment=segment, metrics={}, equity_curve=[])
            s.add(run)
            await s.flush()
            return run.id

    async def submit(self, cfg: BacktestConfig, requested_by: str = "user", **kw) -> int:
        run_id = await self.create_run(cfg, requested_by, **kw)
        task = asyncio.create_task(self.execute(run_id, cfg))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run_id

    async def run_and_wait(self, cfg: BacktestConfig, requested_by: str = "user", **kw) -> tuple[int, dict]:
        run_id = await self.create_run(cfg, requested_by, **kw)
        await self.execute(run_id, cfg)
        return run_id, await self.get_run(run_id)

    async def execute(self, run_id: int, cfg: BacktestConfig) -> BacktestResult | None:
        self.progress[run_id] = 0.0
        async with self.db.session() as s:
            run = await s.get(BacktestRun, run_id)
            run.status = "running"
            run.started_at = utcnow()
        try:
            summary = await dataset_summary(self.db, cfg.data_source, cfg.exchanges, cfg.symbols, cfg.start, cfg.end)
            if summary["snapshots"] == 0:
                raise InsufficientDataError(
                    f"No {cfg.data_source.value} order-book data recorded for {', '.join(cfg.exchanges)} / "
                    f"{', '.join(cfg.symbols)} between {cfg.start.isoformat()} and {cfg.end.isoformat()}. "
                    "Record live market data first (market data mode 'live' with recording enabled), "
                    "or explicitly generate a synthetic dataset for testing.")
            books = await load_books(self.db, cfg.data_source, cfg.exchanges, cfg.symbols, cfg.start, cfg.end)
            gaps = find_gaps(books)
            rules, notes = await self.rules_for(cfg.exchanges, cfg.symbols)
            engine = BacktestEngine(cfg, rules, self.fallback_fx)

            def prog(p: float) -> None:
                self.progress[run_id] = p

            result = await asyncio.to_thread(engine.run, books, len(books), prog)
            result.metrics["warnings"] = list(dict.fromkeys(result.metrics.get("warnings", []) + notes + (
                [f"{len(gaps)} data gaps longer than 30s in the dataset"] if gaps else []) + (
                ["SYNTHETIC DATA: results say nothing about real market profitability"]
                if cfg.data_source == DataSource.SYNTHETIC else [])))
            result.metrics["data_gaps"] = gaps[:50]
            result.metrics["initial_balances"] = result.initial_balances
            result.metrics["final_balances"] = result.final_balances
            async with self.db.session() as s:
                run = await s.get(BacktestRun, run_id)
                run.status = "completed"
                run.progress = 1.0
                run.finished_at = utcnow()
                run.metrics = result.metrics
                run.equity_curve = result.equity_curve
                run.result_hash = result.result_hash
                run.dataset_id = summary["dataset_id"]
                run.dataset_summary = summary
                if result.jobs:
                    await s.execute(insert(BacktestTrade), [{
                        "run_id": run_id, "ts": j.created, "kind": j.opp.kind.value, "status": j.status.value,
                        "route": j.opp.route, "exchanges": j.opp.exchanges, "expected_net_gbp": j.opp.net_profit_gbp,
                        "pnl_gbp": j.pnl_gbp, "fees_gbp": j.fees_gbp, "hedge_loss_gbp": j.hedge_loss_gbp,
                        "notional_gbp": j.opp.notional_gbp, "detail": j.to_dict()} for j in result.jobs])
            return result
        except Exception as e:  # noqa: BLE001
            log.warning("backtest failed", run_id=run_id, error=str(e))
            async with self.db.session() as s:
                run = await s.get(BacktestRun, run_id)
                run.status = "failed"
                run.error = str(e)[:4000]
                run.finished_at = utcnow()
            return None
        finally:
            self.progress.pop(run_id, None)

    async def list_runs(self, limit: int = 100) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(BacktestRun).order_by(BacktestRun.id.desc()).limit(limit))).scalars().all()
        out = [run_summary(r) for r in rows]
        for o in out:
            if o["id"] in self.progress:
                o["progress"] = self.progress[o["id"]]
        return out

    async def get_run(self, run_id: int) -> dict | None:
        async with self.db.session() as s:
            r = await s.get(BacktestRun, run_id)
            if r is None:
                return None
            d = run_summary(r)
            d.update(config=r.config, metrics=r.metrics, equity_curve=r.equity_curve, dataset_summary=r.dataset_summary)
            if run_id in self.progress:
                d["progress"] = self.progress[run_id]
            return d

    async def get_trades(self, run_id: int, limit: int = 1000) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(BacktestTrade).where(BacktestTrade.run_id == run_id)
                                    .order_by(BacktestTrade.ts).limit(limit))).scalars().all()
        return [{"id": t.id, "ts": t.ts.isoformat(), "kind": t.kind, "status": t.status, "route": t.route,
                 "exchanges": t.exchanges, "expected_net_gbp": _dec(t.expected_net_gbp), "pnl_gbp": _dec(t.pnl_gbp),
                 "fees_gbp": _dec(t.fees_gbp), "hedge_loss_gbp": _dec(t.hedge_loss_gbp),
                 "notional_gbp": _dec(t.notional_gbp), "detail": t.detail} for t in rows]

    async def delete_run(self, run_id: int) -> bool:
        async with self.db.session() as s:
            await s.execute(delete(BacktestTrade).where(BacktestTrade.run_id == run_id))
            res = await s.execute(delete(BacktestRun).where(BacktestRun.id == run_id))
            return bool(res.rowcount)

    async def compare(self, ids: list[int]) -> list[dict]:
        out = []
        for i in ids:
            r = await self.get_run(i)
            if r:
                out.append(r)
        return out

    # ------------------------------------------------------------------ synthetic data
    async def generate_synthetic_dataset(self, exchanges: list[str], symbols: list[str], start: datetime,
                                         end: datetime, interval_ms: int = 1000, seed: int = 1234,
                                         dislocation_prob: float = 0.01) -> dict:
        """Write a SYNTHETIC dataset to the snapshot table (labelled source='synthetic')."""
        if end <= start:
            raise ValueError("end must be after start")
        est_rows = (end - start).total_seconds() * 1000 / interval_ms * len(exchanges) * len(symbols)
        if est_rows > 400_000:
            raise ValueError(f"synthetic dataset would contain ~{int(est_rows):,} snapshots (limit 400,000); "
                             "shorten the period, increase the interval or reduce markets")
        books = await asyncio.to_thread(generate_history, exchanges, symbols, start, end, interval_ms, seed, 10,
                                        dislocation_prob)
        rows = [snapshot_row(ob, 10, None) for ob in books]
        async with self.db.session() as s:
            await s.execute(delete(OrderBookSnapshot).where(
                OrderBookSnapshot.source == DataSource.SYNTHETIC.value, OrderBookSnapshot.exchange.in_(exchanges),
                OrderBookSnapshot.symbol.in_(symbols), OrderBookSnapshot.received_ts >= start,
                OrderBookSnapshot.received_ts <= end))
            for i in range(0, len(rows), 5000):
                await s.execute(insert(OrderBookSnapshot), rows[i:i + 5000])
        return {"source": "synthetic", "snapshots": len(rows), "start": start.isoformat(), "end": end.isoformat(),
                "exchanges": exchanges, "symbols": symbols, "seed": seed}


# ====================================================================== walk-forward
GRID_FIELDS = {
    "min_net_profit_pct", "slippage_buffer_bps", "latency_cost_bps", "rebalancing_cost_bps", "leg_failure_prob",
    "leg_failure_loss_bps", "max_trade_gbp",
}
OBJECTIVES = {"total_pnl_gbp", "net_return_pct", "sharpe_ratio", "successful_cycles"}


def split_windows(start: datetime, end: datetime, train_frac: float, val_frac: float):
    total = end - start
    t_end = start + total * train_frac
    v_end = t_end + total * val_frac
    return (start, t_end), (t_end, v_end), (v_end, end)


def _objective(metrics: dict, name: str) -> float:
    v = metrics.get(name)
    if v is None:
        return float("-inf")
    return float(v)


class WalkForwardService:
    """Train / validation / out-of-sample test with a sealed test segment.

    * ``optimise`` evaluates the parameter grid on the training window, re-evaluates
      the top candidates on the validation window and locks the selection.
      It never loads data from the test window.
    * ``run_test`` is only allowed once the selection is locked, evaluates the
      locked parameters exactly once on the test window, and is not exposed to
      AI agents.
    """

    def __init__(self, db: Database, backtests: BacktestService):
        self.db = db
        self.bt = backtests

    async def create(self, base: BacktestConfig, param_grid: dict[str, list], train_frac: float = 0.6,
                     val_frac: float = 0.2, objective: str = "total_pnl_gbp", top_k: int = 3,
                     requested_by: str = "user") -> int:
        if objective not in OBJECTIVES:
            raise ValueError(f"objective must be one of {sorted(OBJECTIVES)}")
        for k in param_grid:
            if k not in GRID_FIELDS:
                raise ValueError(f"parameter {k} cannot be optimised; allowed: {sorted(GRID_FIELDS)}")
        if not (0 < train_frac < 1 and 0 < val_frac < 1 and train_frac + val_frac < 1):
            raise ValueError("invalid split fractions")
        n = 1
        for v in param_grid.values():
            n *= max(1, len(v))
        if n > 64:
            raise ValueError("parameter grid too large (max 64 combinations)")
        async with self.db.session() as s:
            st = WalkForwardStudy(config={"base": base.model_dump(mode="json"), "train_frac": train_frac,
                                          "val_frac": val_frac, "top_k": top_k},
                                  param_grid={k: [str(x) for x in v] for k, v in param_grid.items()},
                                  objective=objective, requested_by=requested_by, status="created",
                                  train_results=[], validation_results=[])
            s.add(st)
            await s.flush()
            return st.id

    async def _get(self, study_id: int) -> WalkForwardStudy:
        async with self.db.session() as s:
            st = await s.get(WalkForwardStudy, study_id)
            if st is None:
                raise KeyError(study_id)
            return st

    def _cfg_for(self, base: BacktestConfig, window, params: dict, label: str) -> BacktestConfig:
        p = StrategyParams(**{**base.params.model_dump(), **{k: Decimal(v) for k, v in params.items()}})
        return base.model_copy(update={"start": window[0], "end": window[1], "params": p,
                                       "name": f"WF {label} {params}"})

    async def optimise(self, study_id: int) -> dict:
        st = await self._get(study_id)
        if st.status not in ("created",):
            raise PermissionError(f"study {study_id} is {st.status}; create a new study to re-optimise")
        base = BacktestConfig(**st.config["base"])
        train_w, val_w, _test_w = split_windows(base.start, base.end, st.config["train_frac"], st.config["val_frac"])
        await self._set(study_id, status="optimising")
        keys = sorted(st.param_grid)
        combos = [dict(zip(keys, vals, strict=True)) for vals in itertools.product(*(st.param_grid[k] for k in keys))]
        train_results = []
        try:
            for combo in combos:
                rid, run = await self.bt.run_and_wait(self._cfg_for(base, train_w, combo, "train"), st.requested_by,
                                                      walkforward_id=study_id, segment="train")
                if run["status"] != "completed":
                    raise RuntimeError(f"training run failed: {run['error']}")
                train_results.append({"params": combo, "run_id": rid, "objective": _objective(run["metrics"], st.objective),
                                      "metrics": run["headline"]})
            ranked = sorted(train_results, key=lambda r: r["objective"], reverse=True)[: st.config["top_k"]]
            val_results = []
            for cand in ranked:
                rid, run = await self.bt.run_and_wait(self._cfg_for(base, val_w, cand["params"], "validation"),
                                                      st.requested_by, walkforward_id=study_id, segment="validation")
                if run["status"] != "completed":
                    raise RuntimeError(f"validation run failed: {run['error']}")
                val_results.append({"params": cand["params"], "run_id": rid,
                                    "objective": _objective(run["metrics"], st.objective), "metrics": run["headline"]})
            best = max(val_results, key=lambda r: r["objective"])
            await self._set(study_id, status="locked", train_results=train_results, validation_results=val_results,
                            selected_params=best["params"], locked_at=utcnow())
        except Exception as e:  # noqa: BLE001
            await self._set(study_id, status="failed", error=str(e)[:4000], train_results=train_results)
            raise
        return await self.get(study_id, include_test=False)

    async def run_test(self, study_id: int) -> dict:
        st = await self._get(study_id)
        if st.status != "locked" or st.selected_params is None:
            raise PermissionError("out-of-sample test can only run once parameters are locked, and only once")
        base = BacktestConfig(**st.config["base"])
        _tw, _vw, test_w = split_windows(base.start, base.end, st.config["train_frac"], st.config["val_frac"])
        rid, run = await self.bt.run_and_wait(self._cfg_for(base, test_w, st.selected_params, "test"), "user",
                                              walkforward_id=study_id, segment="test")
        await self._set(study_id, status="tested", test_result={"run_id": rid, "status": run["status"],
                                                                 "objective": _objective(run.get("metrics") or {}, st.objective),
                                                                 "metrics": run["headline"], "error": run["error"]})
        return await self.get(study_id, include_test=True)

    async def _set(self, study_id: int, **fields) -> None:
        async with self.db.session() as s:
            st = await s.get(WalkForwardStudy, study_id)
            for k, v in fields.items():
                setattr(st, k, v)

    async def get(self, study_id: int, include_test: bool) -> dict:
        st = await self._get(study_id)
        base = st.config["base"]
        windows = split_windows(datetime.fromisoformat(base["start"]), datetime.fromisoformat(base["end"]),
                                st.config["train_frac"], st.config["val_frac"])
        return {
            "id": st.id, "status": st.status, "objective": st.objective, "param_grid": st.param_grid,
            "created_at": st.created_at.isoformat(), "locked_at": st.locked_at.isoformat() if st.locked_at else None,
            "windows": {n: [w[0].isoformat(), w[1].isoformat()] for n, w in zip(("train", "validation", "test"), windows, strict=True)},
            "train_results": st.train_results, "validation_results": st.validation_results,
            "selected_params": st.selected_params,
            "test_result": st.test_result if include_test else ("sealed" if st.test_result else None),
            "error": st.error, "base": base,
        }

    async def list(self, include_test: bool) -> list[dict]:
        async with self.db.session() as s:
            ids = (await s.execute(select(WalkForwardStudy.id).order_by(WalkForwardStudy.id.desc()).limit(50))).scalars().all()
        return [await self.get(i, include_test) for i in ids]
