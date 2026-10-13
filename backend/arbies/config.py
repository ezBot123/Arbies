"""Environment-based configuration (pydantic-settings).

Every setting can be overridden with an ``ARBIES_`` prefixed environment
variable, e.g. ``ARBIES_DATABASE_URL``. Secrets are only ever read here on the
backend and are never serialised to API responses (see ``public_dict``).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class MarketDataMode(StrEnum):
    LIVE = "live"  # connect to real exchanges (public market data, no credentials needed)
    SYNTHETIC = "synthetic"  # generated data for development/demo - clearly labelled everywhere
    OFF = "off"  # no market data (e.g. backtest-only usage)


class OperatingMode(StrEnum):
    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ARBIES_", env_file=".env", extra="ignore")

    env: str = "development"
    log_level: str = "INFO"
    log_json: bool = False

    database_url: str = "postgresql+asyncpg://arbies:arbies@localhost:5432/arbies"
    db_echo: bool = False

    # --- Authentication -------------------------------------------------------------
    admin_username: str = "admin"
    admin_password: SecretStr | None = None  # if unset a random one is generated & logged once
    session_ttl_hours: int = 12
    cookie_secure: bool = False  # set true behind HTTPS
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])

    # --- Credential encryption -------------------------------------------------------
    # Fernet key used to encrypt exchange API credentials at rest.
    secret_key: SecretStr | None = None

    # --- Market data ----------------------------------------------------------------
    market_data_mode: MarketDataMode = MarketDataMode.LIVE
    exchanges: list[str] = Field(default_factory=lambda: ["kraken", "coinbase", "bitstamp"])
    symbols: list[str] = Field(
        default_factory=lambda: ["BTC/USD", "ETH/USD", "ETH/BTC", "BTC/GBP", "ETH/GBP", "USDC/USD"]
    )
    orderbook_depth: int = 25
    max_data_age_ms: int = 5000
    rest_poll_interval_s: float = 2.0
    record_market_data: bool = True
    record_interval_ms: int = 1000  # per-market snapshot throttle for persistence
    record_depth: int = 20
    data_retention_days: int = 30

    # --- Strategy / paper defaults ------------------------------------------------------
    reporting_currency: str = "GBP"
    paper_initial_capital_gbp: Decimal = Decimal("500")
    max_trade_size_gbp: Decimal = Decimal("25")
    min_net_profit_pct: Decimal = Decimal("0.30")
    scan_interval_ms: int = 500
    # Static fallback FX rates to the reporting currency, used ONLY when no market
    # path exists. Values are placeholders and flagged as such in valuations.
    fallback_fx_to_gbp: dict[str, Decimal] = Field(
        default_factory=lambda: {"USD": Decimal("0.75"), "USDC": Decimal("0.75"), "USDT": Decimal("0.75"), "EUR": Decimal("0.85")}
    )

    # --- Live trading safety --------------------------------------------------------------
    # Live trading needs ALL of: this flag, an explicit confirmation flow through the API,
    # per-exchange credentials with trading permission. It is never enabled automatically.
    live_trading_enabled: bool = False
    live_confirmation_phrase: str = "I UNDERSTAND THIS USES REAL MONEY"

    # --- AI providers -------------------------------------------------------------------
    ai_provider: str = "none"  # none | anthropic | openai
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    openai_model: str = "gpt-4o-mini"
    ai_max_tool_calls: int = 12
    ai_monthly_budget_usd: Decimal = Decimal("20")

    def public_dict(self) -> dict:
        """Settings safe to show in the UI (no secrets)."""
        return {
            "env": self.env,
            "market_data_mode": self.market_data_mode.value,
            "exchanges": self.exchanges,
            "symbols": self.symbols,
            "orderbook_depth": self.orderbook_depth,
            "max_data_age_ms": self.max_data_age_ms,
            "record_market_data": self.record_market_data,
            "record_interval_ms": self.record_interval_ms,
            "data_retention_days": self.data_retention_days,
            "reporting_currency": self.reporting_currency,
            "paper_initial_capital_gbp": str(self.paper_initial_capital_gbp),
            "max_trade_size_gbp": str(self.max_trade_size_gbp),
            "min_net_profit_pct": str(self.min_net_profit_pct),
            "live_trading_enabled_by_env": self.live_trading_enabled,
            "ai_provider": self.ai_provider,
            "ai_provider_configured": self.ai_configured,
            "anthropic_model": self.anthropic_model,
            "openai_model": self.openai_model,
            "openai_base_url": self.openai_base_url,
            "credential_encryption_configured": self.secret_key is not None,
        }

    @property
    def ai_configured(self) -> bool:
        if self.ai_provider == "anthropic":
            return self.anthropic_api_key is not None
        if self.ai_provider == "openai":
            return self.openai_api_key is not None or bool(self.openai_base_url)
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
