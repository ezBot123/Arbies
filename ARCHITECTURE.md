# Architecture

## Overview

```
                ┌──────────────────────────── FastAPI process ────────────────────────────┐
 Exchanges ───► │ Market data adapters ──► MarketDataHub ──┬──► Scanner (detectors)      │
 (CCXT/CCXT Pro │  (CcxtAdapter /          latest books,   ├──► Recorder ─► PostgreSQL   │
  public data)  │   SyntheticAdapter)      health, rules   ├──► PaperTrader (TradingCore)│
                │                                          └──► WebSocket broadcaster    │
                │                                                                          │
                │ BacktestService ── dataset loader ──► BacktestEngine (TradingCore) ─► DB │
                │ WalkForwardService (train / validation / sealed test)                    │
                │ AgentRunner ── ToolGateway (permissioned tools) ── optional LLM provider │
                │ RiskEngine (deterministic)   LiveTradingGate / LiveOrderManager (inactive)│
                └──────────────────────────────────────────────────────────────────────────┘
                          ▲ REST /api/*  + WebSocket /ws (session cookie)
                          │
                 React dashboard (Vite, TypeScript, Tailwind, Recharts, TanStack Query)
```

It runs as one Python process: `asyncio` for I/O and threads for CPU-heavy backtests. With a handful of exchanges this is deliberate. It avoids the operational cost of message brokers or separate workers. Redis is not used, because nothing needs cross-process state.

## Components

| Module | Responsibility |
|---|---|
| `arbies/core/models.py` | Normalised `OrderBook`, `BookLevel`, `MarketRules` and the `DataSource` provenance enum. Money is `Decimal`, timestamps are UTC-aware. |
| `arbies/core/valuation.py` | Converts assets to GBP by searching a graph of observed mid prices. Uses static fallback FX only when no market path exists, and flags the result as `estimated`. |
| `arbies/market_data/` | `CcxtAdapter` streams via WebSocket with reconnect/backoff and falls back to REST per symbol. It also does clock sync and loads exchange rules and public fee schedules. `SyntheticAdapter` is a labelled generator. `MarketDataHub` holds the latest books and connection health and fans updates out to subscribers. `MarketDataRecorder` writes throttled, batched snapshots outside the critical path. |
| `arbies/arbitrage/` | `depth.py` does executable VWAP/depth maths. `CrossExchangeDetector` and `TriangularDetector` are pure, deterministic functions from books, balances and parameters to `Opportunity` objects with full cost breakdowns and rejection reasons. |
| `arbies/risk/engine.py` | `RiskEngine`: hard limits, kill switch, circuit breaker, duplicate protection, exposure checks. It contains no AI. |
| `arbies/execution/` | `Portfolio` keeps per-exchange inventory and can never go negative. `ExecutionSimulator` and `ArbitrageExecutor` simulate fills, latency, partial fills and hedging. `live.py` holds the live-trading gate, order validation, a whitelisting `SafeExchange` wrapper, the order manager and reconciliation. |
| `arbies/services/trading_core.py` | `TradingCore` runs detection → risk → simulated execution. **The backtester and the paper trader both use it**, so their strategy logic, risk rules and fill assumptions are identical. |
| `arbies/backtest/` | Config, dataset loading with content hashing, the replay engine, metrics, persistence, comparisons and walk-forward validation. |
| `arbies/paper/` | The forward paper trader, with persistent sessions, equity points, trade ledger and restore-as-paused after a restart. |
| `arbies/agents/` | Tool registry and gateway (permissions), LLM providers (Anthropic SDK, OpenAI-compatible HTTP), the six agents and the runner. |
| `arbies/api/` | REST routes, auth dependencies, WebSocket stream, security headers, SPA hosting. |
| `arbies/db/` | SQLAlchemy 2.0 models (24 tables) and async sessions. Schema changes go through Alembic migrations in `backend/alembic/`. |

## Data flow and timing

1. Adapters normalise each update into an `OrderBook`, stamping it with `received_ts` (local UTC) and the exchange timestamp when there is one.
2. The hub stores the latest book per `(exchange, symbol)` and pushes it to subscriber queues. Each queue is bounded and drops the oldest item, so a slow consumer can never block the feed.
3. Every 500 ms the scanner evaluates all routes. With 3 exchanges × 6 pairs, a scan measured about 2–3 ms. The scanner keeps the current table in memory and records opportunity *episodes* (first appearance and changes in qualification) to the database in batches.
4. The recorder writes one snapshot per market per second (configurable), carrying the top 20 levels, both timestamps, the source label and data-quality flags.
5. The paper trader runs `TradingCore` on the latest books. A simulated order fills against whatever book exists *after* the simulated latency has elapsed.

No LLM call and no synchronous database write sits on the scanning or simulated-execution path.

## Persistence

PostgreSQL holds all state. Financial columns are `NUMERIC(38,18)`, and timestamps are `timestamptz` through a `UTCDateTime` type that rejects naive datetimes. The main tables are:

- market data: `orderbook_snapshots`, `connection_events`, `markets`, `exchanges`;
- detection: `opportunities`;
- backtesting: `backtest_runs` and `backtest_trades`, with config, dataset id, code revision, seed and result hash;
- `walkforward_studies`;
- paper trading: `paper_sessions`, `paper_trades`, `paper_equity`;
- live trading: `live_orders`, `order_fills`, `exchange_credentials` (encrypted);
- agents: `agent_tasks`, `agent_recommendations`;
- operations: `risk_events`, `system_logs`, `audit_logs`, `settings_kv`, `auth_sessions`.

The test suite runs on SQLite by default and on PostgreSQL in CI.

## Key decisions

### CCXT instead of Hummingbot / Condor

These were checked against their current repositories and documentation:

- **Condor** is a Telegram bot and web dashboard for controlling the **Hummingbot API**: bots, orders and stored exchange keys. It is an operator front-end, not an orchestration library, and it is designed to *control real trading*. Integrating it would add a second control surface for funds, which runs against Arbies' rule that AI agents never get execution authority.
- **Hummingbot** (Apache-2.0) is a complete trading runtime with its own connectors, strategy framework and execution loop. Running it next to Arbies would duplicate execution and risk logic. It would also split the "same code for backtest, paper and live" guarantee across two systems.
- **CCXT** (MIT, actively maintained; CCXT Pro WebSocket support has been merged into the free package since v4) provides normalised public market data, market rules and fee schedules for the exchanges we need, with a small integration surface. It was installed and checked as version 4.5.87, with `watchOrderBook` available for kraken, coinbase, bitstamp and gemini.

The decision can be revisited if Arbies ever needs Hummingbot's market-making or DEX connectors.

### Exchange selection (UK)

The default exchanges are Kraken, Coinbase and Bitstamp. At the time of writing all three offer spot trading to UK customers, and their public market-data APIs need no credentials. Binance is not a default, because of UK restrictions on new customers. Eligibility and API permissions can change and depend on your account, so verify them yourself. The dashboard shows a reminder for each exchange.

### Deterministic core, AI at the edges

Scanning, risk checks, execution and accounting are plain deterministic code. Agents analyse, simulate and propose through a narrow tool gateway; see [SECURITY.md](SECURITY.md). Each agent produces a deterministic report from real data even when no LLM is configured.

### Fee model

- **Cross-exchange:** taker fees are charged in each leg's quote currency.
- **Triangular:** fees are charged in the currency received on each leg.

Fee rates come from CCXT's public base-tier schedule, from a configured override, or from a default of 0.26%. The source is recorded on each market (`fee_source`). Account-specific fee tiers need credentials and are not fetched automatically.

## Frontend

React 19 + TypeScript + Vite + Tailwind v4, with a small in-house component kit (`src/components/ui.tsx`) built on CSS design tokens and dark/light themes. Data comes from TanStack Query (REST polling) plus one WebSocket stream for live ticks. Chart colours are resolved from the theme tokens, and the frontend never holds secrets. The session token is an `httpOnly`, `SameSite=Strict` cookie.
