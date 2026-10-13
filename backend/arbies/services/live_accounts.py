"""Exchange account credentials (encrypted) and read-only account access."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import delete, select

from arbies.core.models import D
from arbies.db.models import ExchangeCredential
from arbies.db.session import Database
from arbies.execution.live import SafeExchange
from arbies.security import CredentialVault


class LiveAccounts:
    def __init__(self, db: Database, vault: CredentialVault, exchange_factory=None):
        self.db = db
        self.vault = vault
        self._factory = exchange_factory

    async def store(self, exchange: str, api_key: str, secret: str, password: str | None = None,
                    permissions: str = "read_only", sandbox: bool = True) -> dict:
        if permissions not in ("read_only", "trade"):
            raise ValueError("permissions must be read_only or trade")
        blob = self.vault.encrypt({"apiKey": api_key, "secret": secret, "password": password})
        fp = self.vault.fingerprint(api_key)
        async with self.db.session() as s:
            await s.execute(delete(ExchangeCredential).where(ExchangeCredential.exchange == exchange))
            s.add(ExchangeCredential(exchange=exchange, encrypted_blob=blob, permissions=permissions, sandbox=sandbox,
                                     key_fingerprint=fp))
        return {"exchange": exchange, "permissions": permissions, "sandbox": sandbox, "key_fingerprint": fp}

    async def remove(self, exchange: str) -> bool:
        async with self.db.session() as s:
            res = await s.execute(delete(ExchangeCredential).where(ExchangeCredential.exchange == exchange))
            return bool(res.rowcount)

    async def list(self) -> list[dict]:
        """Metadata only - secrets never leave the backend."""
        async with self.db.session() as s:
            rows = (await s.execute(select(ExchangeCredential))).scalars().all()
        return [{"exchange": r.exchange, "permissions": r.permissions, "sandbox": r.sandbox,
                 "key_fingerprint": r.key_fingerprint, "created_at": r.created_at.isoformat()} for r in rows]

    async def permissions(self) -> dict[str, str]:
        return {r["exchange"]: r["permissions"] for r in await self.list()}

    async def _client(self, exchange: str, allow_trading: bool = False) -> SafeExchange:
        async with self.db.session() as s:
            row = (await s.execute(select(ExchangeCredential).where(ExchangeCredential.exchange == exchange))).scalar_one_or_none()
        if row is None:
            raise KeyError(f"no credentials stored for {exchange}")
        creds = self.vault.decrypt(row.encrypted_blob)
        if self._factory is not None:
            ex = self._factory(exchange, creds)
        else:
            import ccxt.async_support as ccxt_async
            cfg = {"apiKey": creds["apiKey"], "secret": creds["secret"], "enableRateLimit": True}
            if creds.get("password"):
                cfg["password"] = creds["password"]
            ex = getattr(ccxt_async, exchange)(cfg)
            if row.sandbox:
                try:
                    ex.set_sandbox_mode(True)
                except Exception:  # noqa: BLE001 - exchange has no sandbox
                    await ex.close()
                    raise RuntimeError(f"{exchange} has no sandbox environment in CCXT; store credentials with "
                                       "sandbox=false only when you intend to use the production API") from None
        return SafeExchange(ex, allow_trading=allow_trading and row.permissions == "trade")

    async def fetch_balances(self, exchange: str) -> dict[str, Decimal]:
        """Read-only balance fetch."""
        client = await self._client(exchange, allow_trading=False)
        try:
            bal = await client.fetch_balance()
            totals = bal.get("total") or {}
            return {k: D(v) for k, v in totals.items() if v}
        finally:
            await client.close()
