# Development

## Layout

```
backend/
  arbies/
    api/            FastAPI app, routes, auth deps, WebSocket
    agents/         tool registry/gateway, LLM providers, agents + runner
    arbitrage/      depth maths, cross-exchange + triangular detectors, models
    backtest/       config, engine, metrics, dataset loader, service, walk-forward
    core/           normalised models, valuation
    db/             SQLAlchemy models, async session
    execution/      portfolio, simulator/executor, live trading infrastructure
    market_data/    CCXT adapter, synthetic adapter, hub, recorder
    paper/          paper trader
    risk/           deterministic risk engine
    services/       trading core, scanner, runtime, strategies, live accounts
    config.py, security.py, logging_setup.py
  alembic/          migrations
  tests/            pytest suite
frontend/
  src/api.ts, hooks.ts, components/, pages/, lib/
docker/, Dockerfile, docker-compose.yml, Makefile, .env.example
```

## Setup

```bash
make install                # creates backend/.venv (pip install -e ".[dev]") and runs npm ci
cp .env.example .env        # set ARBIES_ADMIN_PASSWORD, ARBIES_DATABASE_URL
createdb arbies && make migrate
make dev-backend            # or: make dev-synthetic (no exchange access needed)
make dev-frontend           # Vite dev server proxies /api and /ws to :8000
```

`uv` works too: `cd backend && uv venv && uv pip install -e ".[dev]"`.

## Configuration

All settings are in `backend/arbies/config.py`. Each one can be overridden by an `ARBIES_*` environment variable. List values use JSON, for example `ARBIES_EXCHANGES='["kraken","coinbase"]'`. Some settings can also be changed at runtime from the UI; those are persisted in `settings_kv`. They are the scanner parameters, data retention, and the risk limits of the paper session and of live mode.

Market data modes:

- `live`: real public exchange data, no keys needed;
- `synthetic`: labelled generated data;
- `off`: no market data, for example backtest-only use.

## Tests

```bash
make test                   # SQLite, ~30 s
make test-pg                # PostgreSQL; set ARBIES_TEST_DATABASE_URL to a disposable database
```

| File | Covers |
|---|---|
| `test_depth.py` | VWAP / depth walking, limit prices, crossing depth |
| `test_cross_exchange.py` | Exact cost arithmetic, thresholds, staleness, inventory, depth, health, minimums, FX |
| `test_triangular.py` | Cycle discovery, sequential simulation, fees, lot size, liquidity |
| `test_portfolio_risk_sim.py` | Balance enforcement, ledger, transfers, valuation, every risk check, executions (success, partial fill + hedge, hold policy, rejected legs, triangular unwind), seeded determinism |
| `test_backtest.py` | Metrics, reproducibility, look-ahead guard, no leakage, fills at execution-time books |
| `test_services.py` | Backtest service end to end with persistence, walk-forward sealing, recorder → replay round trip, paper trader lifecycle |
| `test_market_data.py` | CCXT adapter with a mocked exchange: normalisation, rules, REST fallback, unsupported symbols, load failures |
| `test_security_agents_live.py` | Agent tool permissions, gateway denial, LLM tool loop cannot escape, deterministic agent runs, sealed walk-forward, every live gate, SafeExchange, order validation, order manager, reconciliation, vault, rate limiter |
| `test_api.py` | Auth, rate limit, all main endpoints, backtest via API, strategy versioning, live arming refused, kill switch, agents, paper |

Unit tests use synthetic fixtures. External APIs are mocked, so no test touches the network.

## Migrations

```bash
cd backend
.venv/bin/alembic revision --autogenerate -m "describe change"
.venv/bin/alembic upgrade head
.venv/bin/alembic check      # fails if models and migrations diverge
```

## Conventions

- **Money:** `Decimal` throughout; API responses send it as strings. **Time:** timezone-aware UTC only (the DB type rejects naive values).
- **Determinism:** scanning, risk and accounting code is pure and deterministic. Randomness only goes through seeded RNGs.
- **Simulation labelling:** every simulated figure is labelled; never present an estimate as realised.
- **Synthetic data labelling:** synthetic data must always carry `DataSource.SYNTHETIC`.
- **Linting:** `ruff check` (Python), `tsc -b` + ESLint (frontend).

## Driving the UI

The dashboard can be checked with Playwright against `make dev-synthetic` and a production build served by the backend (`npm run build`, then open `http://localhost:8000`). During development every page was screenshotted, and a full backtest was run through the UI this way.
