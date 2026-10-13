"""Versioned strategy configurations. Every change creates a new immutable version."""

from __future__ import annotations

from sqlalchemy import select, update

from arbies.arbitrage.models import StrategyParams
from arbies.db.models import StrategyConfig
from arbies.db.session import Database


def _row(r: StrategyConfig) -> dict:
    return {"id": r.id, "name": r.name, "version": r.version, "kind": r.kind, "params": r.params,
            "enabled_in_simulation": r.enabled_in_simulation, "is_current": r.is_current, "note": r.note,
            "created_by": r.created_by, "created_at": r.created_at.isoformat()}


class StrategyStore:
    KINDS = ("cross_exchange", "triangular")

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def validate(kind: str, params: dict) -> dict:
        if kind not in StrategyStore.KINDS:
            raise ValueError(f"kind must be one of {StrategyStore.KINDS}")
        sp = StrategyParams(**(params.get("strategy") or {}))
        return {"strategy": sp.model_dump(mode="json"), "exchanges": list(params.get("exchanges") or []),
                "symbols": list(params.get("symbols") or [])}

    async def seed_defaults(self, exchanges: list[str], symbols: list[str]) -> None:
        async with self.db.session() as s:
            exists = (await s.execute(select(StrategyConfig.id).limit(1))).first()
        if exists:
            return
        await self.create("Cross-exchange default", "cross_exchange",
                          {"strategy": {}, "exchanges": exchanges, "symbols": symbols},
                          "Initial research defaults (not a profitability claim)", "system", enabled=True)
        await self.create("Triangular default", "triangular",
                          {"strategy": {}, "exchanges": exchanges, "symbols": symbols},
                          "Initial research defaults (not a profitability claim)", "system", enabled=True)

    async def create(self, name: str, kind: str, params: dict, note: str = "", created_by: str = "user",
                     enabled: bool = False) -> dict:
        params = self.validate(kind, params)
        async with self.db.session() as s:
            if (await s.execute(select(StrategyConfig.id).where(StrategyConfig.name == name))).first():
                raise ValueError(f"strategy '{name}' already exists")
            r = StrategyConfig(name=name, version=1, kind=kind, params=params, note=note, created_by=created_by,
                               enabled_in_simulation=enabled, is_current=True)
            s.add(r)
            await s.flush()
            return _row(r)

    async def _current(self, s, name: str) -> StrategyConfig:
        r = (await s.execute(select(StrategyConfig).where(StrategyConfig.name == name, StrategyConfig.is_current.is_(True)))).scalar_one_or_none()
        if r is None:
            raise KeyError(name)
        return r

    async def update(self, name: str, params: dict, note: str = "", created_by: str = "user") -> dict:
        async with self.db.session() as s:
            cur = await self._current(s, name)
            new_params = self.validate(cur.kind, params)
            await s.execute(update(StrategyConfig).where(StrategyConfig.name == name).values(is_current=False))
            r = StrategyConfig(name=name, version=cur.version + 1, kind=cur.kind, params=new_params, note=note,
                               created_by=created_by, enabled_in_simulation=cur.enabled_in_simulation, is_current=True)
            s.add(r)
            await s.flush()
            return _row(r)

    async def restore(self, name: str, version: int, created_by: str = "user") -> dict:
        async with self.db.session() as s:
            old = (await s.execute(select(StrategyConfig).where(StrategyConfig.name == name,
                                                                StrategyConfig.version == version))).scalar_one_or_none()
            if old is None:
                raise KeyError(f"{name} v{version}")
            params = dict(old.params)
        return await self.update(name, params, f"Restored from version {version}", created_by)

    async def set_enabled(self, name: str, enabled: bool) -> dict:
        async with self.db.session() as s:
            cur = await self._current(s, name)
            cur.enabled_in_simulation = enabled
            return _row(cur)

    async def list_current(self) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(StrategyConfig).where(StrategyConfig.is_current.is_(True))
                                    .order_by(StrategyConfig.name))).scalars().all()
        return [_row(r) for r in rows]

    async def versions(self, name: str) -> list[dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(StrategyConfig).where(StrategyConfig.name == name)
                                    .order_by(StrategyConfig.version.desc()))).scalars().all()
        return [_row(r) for r in rows]

    async def get(self, name: str, version: int | None = None) -> dict:
        async with self.db.session() as s:
            if version is None:
                return _row(await self._current(s, name))
            r = (await s.execute(select(StrategyConfig).where(StrategyConfig.name == name,
                                                              StrategyConfig.version == version))).scalar_one_or_none()
            if r is None:
                raise KeyError(f"{name} v{version}")
            return _row(r)
