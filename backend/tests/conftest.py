import os
from datetime import UTC, datetime, timedelta

import pytest

from arbies.config import MarketDataMode, Settings
from arbies.core.models import utcnow
from arbies.db.session import Database
from arbies.market_data.synthetic import SyntheticAdapter, SyntheticWorld

# Integration tests run against SQLite by default; set ARBIES_TEST_DATABASE_URL to use PostgreSQL.
TEST_DB = os.environ.get("ARBIES_TEST_DATABASE_URL")


@pytest.fixture
async def db(tmp_path):
    if TEST_DB:
        d = Database(TEST_DB)
        from arbies.db.models import Base
        async with d.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    else:
        d = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
        await d.create_all()
    yield d
    await d.dispose()


@pytest.fixture
def settings():
    return Settings(_env_file=None, market_data_mode=MarketDataMode.OFF, admin_password="test-password-1",
                    exchanges=["kraken", "coinbase"], symbols=["BTC/USD", "ETH/USD", "ETH/BTC"],
                    database_url="sqlite+aiosqlite://", record_market_data=False, ai_provider="none")


def feed_synthetic(hub, exchanges=("kraken", "coinbase"), symbols=("BTC/USD", "ETH/USD", "ETH/BTC"), seed=5):
    """Push one round of synthetic books into a hub (marks adapters as healthy)."""
    world = SyntheticWorld(seed)
    now = utcnow()
    for ex in exchanges:
        ad = hub.adapters.get(ex)
        if ad is None:
            ad = SyntheticAdapter(ex, list(symbols), hub.on_book, hub.on_event, world, seed=seed)
            hub.add_adapter(ad)
            hub._refresh_rules(ex)
            from arbies.market_data.base import ConnStatus
            ad.health.status = ConnStatus.CONNECTED
        for s in ad.symbols:
            ad.health.last_update[s] = now
            hub.on_book(ad.make_book(s, now))


T_START = datetime(2026, 2, 1, tzinfo=UTC)
T_END = T_START + timedelta(minutes=30)
