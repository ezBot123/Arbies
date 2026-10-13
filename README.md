# Arbies

Arbies is a research platform for cryptocurrency arbitrage. It is built to answer one question honestly: **do statistically repeatable arbitrage opportunities exist once every realistic cost is counted?**

It does this by:

- streaming live order books from several exchanges;
- detecting cross-exchange and triangular opportunities at executable, depth-walked prices, net of fees and modelled costs;
- recording the market data so it can be replayed;
- backtesting on that data, with latency, partial fills, failed hedges, inventory limits and walk-forward validation;
- paper trading on current data with the same code;
- giving you six AI agents that analyse results through permissioned tools.

> **Paper trading is the default and live trading is disabled.** Arbies never assumes profitability. Every estimate is labelled as an estimate, every simulated result as simulated, and synthetic data is labelled wherever it appears.

## Status

Initial research release. Milestones 1–8 are implemented. **Milestone 7, live trading preparation, is deliberately left inactive:** the order-management, gating and reconciliation code exists and is tested, but automated real-money execution of strategies is not wired up. See [PROGRESS.md](PROGRESS.md) for exactly what is done, what has been verified, and what remains.

**Live public market data is verified end to end** (2026-10-13). The three exchanges were Kraken, Coinbase and Bitstamp, with real order-book streams recorded to PostgreSQL. The dashboard was checked against the books, and a £500 paper session ran on that data. See PROGRESS.md, Session 5. Two caveats remain. The fee rates are CCXT defaults, not your account's tier: check them and set `fee_overrides`. Live trading is still disabled. `ARBIES_MARKET_DATA_MODE=live` needs no API keys.

**Historical investment simulator** (Session 6): £500 + £100/month from Jan 2025 replayed on 21M genuine Tardis order books (22 free first-of-month days). Result: no arbitrage trades cleared retail fees; see [HISTORICAL_SIMULATION.md](HISTORICAL_SIMULATION.md).

## Quick start (one command)

On your own computer (macOS, Linux or WSL2) with internet access:

```bash
git clone https://github.com/ezBot123/Arbies && cd Arbies
git checkout claude/vigilant-ptolemy-9yuhtw
./scripts/setup_local.sh            # or: ./scripts/setup_local.sh --docker  (PostgreSQL in Docker)
make dev-backend                    # dashboard + API on http://localhost:8000
```

The script checks prerequisites, creates a git-ignored `.env` with generated secrets (live trading **off**), installs dependencies, prepares PostgreSQL, applies migrations, runs the tests and finally verifies the real exchange feeds. After the backend has run for a few minutes, confirm that real data is being recorded:

```bash
cd backend && .venv/bin/python -m arbies.tools.verify_live --recording
```

The verifier uses public endpoints only, never falls back to synthetic data and prints PASS/FAIL per exchange with the actual error.

## Quick start (manual)

Requirements: Python 3.12+, Node 22+ and PostgreSQL 16 (or Docker).

```bash
cp .env.example .env           # edit: set ARBIES_ADMIN_PASSWORD and the database URL
make install                   # backend venv + frontend node_modules
createdb arbies                # or use docker compose (below)
make migrate                   # alembic upgrade head
make dev-backend               # API on http://localhost:8000 (live public market data)
make dev-frontend              # dashboard on http://localhost:5173
```

You can work without exchange access, for example offline or for UI work. Run `make dev-synthetic` instead of `make dev-backend`. Synthetic mode is labelled **SYNTHETIC DATA** throughout the UI.

If `ARBIES_ADMIN_PASSWORD` is not set, a random admin password is generated on first start and printed once in the backend log.

### Docker

```bash
cp .env.example .env           # set POSTGRES_PASSWORD, ARBIES_ADMIN_PASSWORD, ARBIES_SECRET_KEY
make docker                    # builds the image and runs Postgres + Arbies on http://localhost:8000
```

The container applies database migrations on start and serves both the API and the built dashboard. See [DEPLOYMENT.md](DEPLOYMENT.md).

## Testing

```bash
make test        # 81 backend tests (SQLite)
make test-pg     # the same suite against PostgreSQL
make lint        # ruff + TypeScript + ESLint
make audit       # pip-audit + npm audit
```

## Documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, data flow, technology decisions (including why Hummingbot/Condor are not used) |
| [DEVELOPMENT.md](DEVELOPMENT.md) | Setup, project layout, configuration, testing, conventions |
| [BACKTESTING.md](BACKTESTING.md) | Replay engine, fill model, metrics, walk-forward validation, reproducibility |
| [HISTORICAL_SIMULATION.md](HISTORICAL_SIMULATION.md) | £500 + £100/month historical simulator: data sources, importer, fees, benchmarks, validation |
| [STRATEGIES.md](STRATEGIES.md) | Cross-exchange and triangular maths, cost model, rejection reasons |
| [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md) | Deterministic risk engine, limits, kill switch, circuit breaker |
| [SECURITY.md](SECURITY.md) | Authentication, secrets, live-trading gates, agent permissions |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Docker, VPS deployment, monitoring, backups |
| [PROGRESS.md](PROGRESS.md) | Milestone status, verification record, known limitations, next steps |

## Default research parameters

| Parameter | Default |
|---|---|
| Paper portfolio | £500 (cash spread across fiat quote currencies, half in base assets, split evenly across exchanges) |
| Primary / secondary strategy | Cross-exchange spot / triangular |
| Pairs | BTC/USD, ETH/USD, ETH/BTC, BTC/GBP, ETH/GBP, USDC/USD |
| Reporting currency | GBP |
| Max paper trade | £25 |
| Min estimated net profit | 0.30% |
| Leverage / live trading / withdrawals / AI execution authority | Disabled / disabled / never / disabled |

These defaults are placeholders for research. They are not claims that these settings are profitable or safe.

## Disclaimer

Arbies is research software. Simulated and backtested results do not predict live results. Cryptocurrency trading carries substantial risk of loss. Before using any exchange, check that it is permitted for you and that its terms and API restrictions allow your use.
