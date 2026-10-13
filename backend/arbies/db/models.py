"""Persistent storage schema.

Financial values use NUMERIC(38, 18). Timestamps are timezone-aware UTC.
Market-data rows carry both the local receive timestamp and the exchange
timestamp, plus a ``source`` column distinguishing real recorded, imported and
synthetic data.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from arbies.core.models import utcnow


class UTCDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on every backend (SQLite drops tzinfo otherwise)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("naive datetimes are not allowed; use UTC-aware datetimes")
            value = value.astimezone(UTC)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


Money = Numeric(38, 18)
TS = UTCDateTime()
# BIGINT autoincrement on Postgres, INTEGER on SQLite (needed for rowid autoincrement)
BigId = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    type_annotation_map = {Decimal: Money, datetime: TS, dict: JSON, list: JSON}


class Exchange(Base):
    __tablename__ = "exchanges"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # ccxt id
    name: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    has_websocket: Mapped[bool] = mapped_column(Boolean, default=False)
    uk_eligibility_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Market(Base):
    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("exchange", "symbol"),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    base: Mapped[str] = mapped_column(String(16))
    quote: Mapped[str] = mapped_column(String(16))
    taker_fee: Mapped[Decimal | None]
    maker_fee: Mapped[Decimal | None]
    fee_source: Mapped[str] = mapped_column(String(32), default="default")
    amount_step: Mapped[Decimal | None]
    price_step: Mapped[Decimal | None]
    min_amount: Mapped[Decimal | None]
    min_cost: Mapped[Decimal | None]
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class OrderBookSnapshot(Base):
    __tablename__ = "orderbook_snapshots"
    __table_args__ = (
        Index("ix_obs_source_recv", "source", "received_ts"),
        Index("ix_obs_market_recv", "exchange", "symbol", "received_ts"),
    )
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))  # DataSource value
    received_ts: Mapped[datetime]
    exchange_ts: Mapped[datetime | None]
    sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    best_bid: Mapped[Decimal | None]
    best_ask: Mapped[Decimal | None]
    bids: Mapped[list]  # [[price, amount], ...] as strings
    asks: Mapped[list]
    depth: Mapped[int] = mapped_column(Integer)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)  # e.g. {"crossed": false, "gap_ms": 120}
    # provenance of imported rows (historical_datasets.id); NULL for live-recorded / synthetic rows
    dataset_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)


class ConnectionEvent(Base):
    __tablename__ = "connection_events"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True)
    event: Mapped[str] = mapped_column(String(32))  # connected | disconnected | error | reconnecting | resync | fallback_rest | stale
    detail: Mapped[str] = mapped_column(Text, default="")


class OpportunityRecord(Base):
    __tablename__ = "opportunities"
    __table_args__ = (Index("ix_opp_detected", "detected_at"), Index("ix_opp_kind_qual", "kind", "qualified"))
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(200), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    detected_at: Mapped[datetime]
    first_seen: Mapped[datetime]
    exchanges: Mapped[list]
    symbols: Mapped[list]
    route: Mapped[list]
    profit_currency: Mapped[str] = mapped_column(String(16))
    gross_spread_pct: Mapped[Decimal]
    gross_profit: Mapped[Decimal]
    fees: Mapped[Decimal]
    total_costs: Mapped[Decimal]
    net_profit: Mapped[Decimal]
    net_profit_gbp: Mapped[Decimal | None]
    net_return_pct: Mapped[Decimal]
    notional_gbp: Mapped[Decimal | None]
    size_base: Mapped[Decimal]
    max_size_base: Mapped[Decimal]
    data_age_ms: Mapped[float] = mapped_column(Float)
    confidence: Mapped[float] = mapped_column(Float)
    qualified: Mapped[bool] = mapped_column(Boolean)
    rejection_reasons: Mapped[list]
    data_source: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict]


class StrategyConfig(Base):
    __tablename__ = "strategy_configs"
    __table_args__ = (UniqueConstraint("name", "version"),)
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    params: Mapped[dict]
    enabled_in_simulation: Mapped[bool] = mapped_column(Boolean, default=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="user")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|completed|failed
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    config: Mapped[dict]  # full BacktestConfig (reproducible inputs)
    strategy_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    strategy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    code_revision: Mapped[str] = mapped_column(String(64), default="unknown")
    dataset_id: Mapped[str] = mapped_column(String(64), default="")
    dataset_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    data_source: Mapped[str] = mapped_column(String(32), default="")
    seed: Mapped[int] = mapped_column(Integer, default=42)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    equity_curve: Mapped[list] = mapped_column(JSON, default=list)
    result_hash: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[str] = mapped_column(String(64), default="user")
    walkforward_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    walkforward_segment: Mapped[str | None] = mapped_column(String(16), nullable=True)


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    route: Mapped[list]
    exchanges: Mapped[list]
    expected_net_gbp: Mapped[Decimal | None]
    pnl_gbp: Mapped[Decimal]
    fees_gbp: Mapped[Decimal]
    hedge_loss_gbp: Mapped[Decimal]
    notional_gbp: Mapped[Decimal | None]
    detail: Mapped[dict]


class WalkForwardStudy(Base):
    __tablename__ = "walkforward_studies"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    status: Mapped[str] = mapped_column(String(16), default="created")  # created|optimising|locked|tested|failed
    config: Mapped[dict]
    param_grid: Mapped[dict]
    objective: Mapped[str] = mapped_column(String(32), default="net_return_pct")
    train_results: Mapped[list] = mapped_column(JSON, default=list)
    validation_results: Mapped[list] = mapped_column(JSON, default=list)
    selected_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    locked_at: Mapped[datetime | None]
    test_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # sealed until locked
    requested_by: Mapped[str] = mapped_column(String(64), default="user")
    error: Mapped[str] = mapped_column(Text, default="")


class PaperSession(Base):
    __tablename__ = "paper_sessions"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), default="Paper session")
    status: Mapped[str] = mapped_column(String(16), default="stopped")  # running|paused|stopped
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    stopped_at: Mapped[datetime | None]
    config: Mapped[dict]
    initial_value_gbp: Mapped[Decimal]
    balances: Mapped[list]  # latest balances [{exchange, asset, amount}]
    data_source: Mapped[str] = mapped_column(String(32), default="")


class PaperTrade(Base):
    __tablename__ = "paper_trades"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("paper_sessions.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    route: Mapped[list]
    exchanges: Mapped[list]
    expected_net_gbp: Mapped[Decimal | None]
    pnl_gbp: Mapped[Decimal]
    fees_gbp: Mapped[Decimal]
    hedge_loss_gbp: Mapped[Decimal]
    notional_gbp: Mapped[Decimal | None]
    detail: Mapped[dict]


class PaperEquityPoint(Base):
    __tablename__ = "paper_equity"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("paper_sessions.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime]
    value_gbp: Mapped[Decimal]
    realised_pnl_gbp: Mapped[Decimal]
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)


class LiveOrder(Base):
    __tablename__ = "live_orders"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    order_type: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal]
    price: Mapped[Decimal | None]
    status: Mapped[str] = mapped_column(String(16))
    filled: Mapped[Decimal] = mapped_column(default=Decimal(0))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    opportunity_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class OrderFill(Base):
    __tablename__ = "order_fills"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("live_orders.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime]
    amount: Mapped[Decimal]
    price: Mapped[Decimal]
    fee: Mapped[Decimal]
    fee_currency: Mapped[str] = mapped_column(String(16))
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    mode: Mapped[str] = mapped_column(String(16))  # paper | live
    session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    balances: Mapped[list]
    value_gbp: Mapped[Decimal]
    estimated: Mapped[bool] = mapped_column(Boolean, default=False)


class ExchangeCredential(Base):
    __tablename__ = "exchange_credentials"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32), unique=True)
    encrypted_blob: Mapped[str] = mapped_column(Text)  # Fernet-encrypted JSON {apiKey, secret, password}
    permissions: Mapped[str] = mapped_column(String(16), default="read_only")  # read_only | trade
    sandbox: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    key_fingerprint: Mapped[str] = mapped_column(String(16), default="")


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|completed|failed
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    finished_at: Mapped[datetime | None]
    input: Mapped[dict] = mapped_column(JSON, default=dict)
    output: Mapped[dict] = mapped_column(JSON, default=dict)
    tool_calls: Mapped[list] = mapped_column(JSON, default=list)
    llm_used: Mapped[bool] = mapped_column(Boolean, default=False)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    est_cost_usd: Mapped[Decimal] = mapped_column(default=Decimal(0))
    error: Mapped[str] = mapped_column(Text, default="")


class AgentRecommendation(Base):
    __tablename__ = "agent_recommendations"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("agent_tasks.id", ondelete="SET NULL"), nullable=True)
    agent: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    title: Mapped[str] = mapped_column(String(200))
    detail: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32))  # research|strategy|risk|performance|health
    severity: Mapped[str] = mapped_column(String(16), default="info")
    proposed_change: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|approved|rejected|dismissed
    decided_at: Mapped[datetime | None]
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RiskEventRecord(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    mode: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class SystemLog(Base):
    __tablename__ = "system_logs"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(16))
    component: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(64))  # user | agent:<name> | system
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class SettingKV(Base):
    __tablename__ = "settings_kv"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ============================================================================ historical data and simulation
class HistoricalDataset(Base):
    """Provenance and import state of one external historical file (audit trail)."""

    __tablename__ = "historical_datasets"
    __table_args__ = (UniqueConstraint("sha256", "sample_interval_ms", "depth", name="uq_hist_dataset_content"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64))  # e.g. tardis.dev
    data_type: Mapped[str] = mapped_column(String(64))  # incremental_book_L2 | book_snapshot_25 | quotes
    exchange: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str] = mapped_column(String(32))  # unified, e.g. BTC/USD
    raw_symbol: Mapped[str] = mapped_column(String(64), default="")
    file_name: Mapped[str] = mapped_column(String(512))
    source_url: Mapped[str] = mapped_column(Text, default="")
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sample_interval_ms: Mapped[int] = mapped_column(Integer, default=1000)
    depth: Mapped[int] = mapped_column(Integer, default=25)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|importing|completed|failed
    rows_read: Mapped[int] = mapped_column(BigInteger, default=0)
    snapshots_written: Mapped[int] = mapped_column(BigInteger, default=0)
    checkpoint_line: Mapped[int] = mapped_column(BigInteger, default=0)  # resume point (data lines consumed)
    checkpoint_state: Mapped[dict] = mapped_column(JSON, default=dict)  # reconstructed book state at checkpoint
    first_exchange_ts: Mapped[datetime | None]
    last_exchange_ts: Mapped[datetime | None]
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None]


class ReferencePrice(Base):
    """Exchange OHLC candles used ONLY for valuation, deposits, benchmarks and screening - never as
    executable arbitrage prices."""

    __tablename__ = "reference_prices"
    __table_args__ = (UniqueConstraint("exchange", "symbol", "interval", "ts", name="uq_refprice"),
                      Index("ix_refprice_lookup", "exchange", "symbol", "interval", "ts"))
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    interval: Mapped[str] = mapped_column(String(8))  # 1m | 1d
    ts: Mapped[datetime]  # candle open time (UTC)
    open: Mapped[Decimal]
    high: Mapped[Decimal]
    low: Mapped[Decimal]
    close: Mapped[Decimal]
    volume: Mapped[Decimal]
    source_url: Mapped[str] = mapped_column(String(256), default="")
    fetched_at: Mapped[datetime] = mapped_column(default=utcnow)


class HistoricalSimulation(Base):
    __tablename__ = "historical_simulations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|completed|failed
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    config: Mapped[dict]
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    result_hash: Mapped[str] = mapped_column(String(64), default="")
    code_revision: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[str] = mapped_column(String(64), default="user")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class HistoricalSimTrade(Base):
    __tablename__ = "historical_sim_trades"
    id: Mapped[int] = mapped_column(BigId, primary_key=True, autoincrement=True)
    simulation_id: Mapped[int] = mapped_column(ForeignKey("historical_simulations.id", ondelete="CASCADE"), index=True)
    variant: Mapped[str] = mapped_column(String(64))  # strategy variant within the simulation
    ts: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    route: Mapped[list]
    exchanges: Mapped[list]
    pnl_gbp: Mapped[Decimal]
    fees_gbp: Mapped[Decimal]
    hedge_loss_gbp: Mapped[Decimal]
    notional_gbp: Mapped[Decimal | None]
    detail: Mapped[dict]
