# Arbies — progress record

This file is kept up to date so development can resume in a later session.

## Milestones

| # | Milestone | State | Notes |
|---|---|---|---|
| 1 | Foundation | ✅ Done | FastAPI backend, React dashboard, PostgreSQL + Alembic, Docker / Compose, config, structured logging, Makefile, CI workflow, tests |
| 2 | Market data | ✅ Implemented · ⚠️ live connection not verified | CCXT Pro WebSocket adapter with REST fallback, reconnect/backoff, clock sync, rules and fees, health monitoring, recorder, live stream to the dashboard. Verified only with mocked exchanges and synthetic data (see Environment limitations). |
| 3 | Arbitrage detection | ✅ Done | Cross-exchange and triangular detectors, executable depth-walked prices, full cost model, rejection reasons, opportunity episodes recorded |
| 4 | Backtesting | ✅ Done | Replay engine, fill/latency/partial-fill/hedge simulation, inventory and rebalancing, metrics, comparison, walk-forward validation with sealed test, UI lab |
| 5 | Paper trading | ✅ Done | Persistent sessions, same `TradingCore` as backtests, start/pause/stop/reset, ledger, equity, restore-as-paused after restart |
| 6 | Multi-agent intelligence | ✅ Done | Six agents with deterministic analyses on real system data; optional Anthropic or OpenAI-compatible LLM narrative; permissioned tool gateway; recommendations need a human decision; cost tracking and budget |
| 7 | Live trading preparation | ✅ Infrastructure only · 🔒 inactive by design | Encrypted credentials, read-only balance access, live gate (env + arming + trade-permission credentials), order pre-validation, idempotent client ids, `SafeExchange` whitelist, emergency cancel-all, reconciliation, audit. Strategies are **not** wired to live execution. |
| 8 | Hardening and deployment | ✅ Done | Migration round-trip validated, `alembic check` clean, Docker image built and run, dependency audits clean, deployment docs |

## Definition of Done — verification record

Status as of the last session.

| Requirement | Status | Evidence |
|---|---|---|
| Application launches locally | ✅ | Backend + dashboard run against PostgreSQL; Docker image runs with migrations on start |
| ≥ 2 eligible exchange market-data connections work | ⚠️ **Not verified** | The dev sandbox network policy returned 403 for every exchange API. The adapter is tested with a mocked CCXT exchange. **Run once with internet access to verify.** |
| Live order books displayed | ⚠️ Synthetic only | The order-book table and stream work; real feeds are unverified for the same reason |
| Cross-exchange opportunities detected | ✅ | Unit tests with exact arithmetic; live scanner on synthetic feeds |
| Triangular opportunities detected | ✅ | Unit tests; live scanner on synthetic feeds |
| Realistic fee and liquidity estimates | ✅ | Depth-walked VWAP, per-exchange fees, costs itemised |
| Historical data recorded and persisted | ✅ | Recorder writes throttled snapshots; recorder → loader round-trip test |
| Backtests can be executed | ✅ | Via API, service and UI (Playwright-driven run: 64,800 events, 11 trades) |
| Backtest results reproducible | ✅ | Identical result hash across runs (engine and service tests) |
| Paper trading works against live market data | ⚠️ Synthetic only | Works against the hub's current books; real feeds unverified |
| Realistic portfolio accounting | ✅ | Per-exchange balances, atomic trades, no negative balances, fees, transfers in transit, frozen-mark P&L |
| AI research agents function | ✅ deterministic · ⚠️ LLM | All six run on real system data. The LLM loop was tested with a scripted fake client only; no API key was available. |
| Strategy comparisons work | ✅ | Compare tab and API; reports by strategy and exchange |
| Risk controls tested | ✅ | 20+ risk, gate and permission tests |
| Dashboard displays real application data | ✅ | Every page screenshotted against a running backend; no hard-coded metrics |
| Tests run successfully | ✅ | 81 passed on SQLite and on PostgreSQL 16 |
| Documentation complete | ✅ | README, ARCHITECTURE, DEVELOPMENT, BACKTESTING, STRATEGIES, RISK_MANAGEMENT, SECURITY, DEPLOYMENT |
| Live trading remains disabled | ✅ | Env flag defaults to false; arming refused in tests; no strategy → live path |

**Overall: not yet declared complete.** Real exchange connectivity (Kraken, Coinbase and Bitstamp public feeds) still needs to be verified on a machine with internet access. Everything else above has been verified.

## Session 2 — local setup and live-data verification (2026-10-13)

Work done:
- Added `scripts/setup_local.sh` (prerequisites → `.env` with generated secrets and live trading off → deps → PostgreSQL → migrations → tests → live-feed check). Tested end to end from a clean clone.
- Added `python -m arbies.tools.verify_live [--recording]`: public-endpoint check per exchange (markets, REST order books, WebSocket updates, clock offset) plus a DB check for `live_recorded` snapshots. It never uses synthetic data.
- Fixed: `.env` at the repo root (the documented location) was ignored when the backend ran from `backend/`.
- Fixed: the setup flow sourced `.env` into the shell, which mangled JSON list values; inline comments were also moved out of `.env.example` values.
- Fixed: a paper session recorded on synthetic data could be restored after switching to live data. Sessions are now only restored on the same data provenance (regression test added).
- Fixed: the header badge said "LIVE DATA" before any real book had arrived; it now shows "NO DATA" until real data exists.
- Fixed: the exchange-health table overflowed its card on the Overview page.

Verified in the cloud development container:

| Check | Result |
|---|---|
| PostgreSQL 16 running; migrations at head; `alembic check` clean | ✅ |
| Backend tests | ✅ 81 passed |
| Frontend type-check and production build | ✅ |
| Backend starts in **live** mode, auth works, dashboard served | ✅ |
| Live trading disabled (`ARBIES_LIVE_TRADING_ENABLED=false`, not armed) | ✅ |
| Exchange feeds (kraken, coinbase, bitstamp) | ❌ **Blocked** — the container's network policy returns 403 for the exchange hosts |
| Live-data recording | ❌ Not possible without feeds (0 rows recorded; nothing synthetic substituted) |
| Dashboard truthfully shows the outage ("NO DATA", 0/3 healthy, real error messages) | ✅ |

Hosts the public feeds need: `api.kraken.com`, `ws.kraken.com`, `api.coinbase.com`, `advanced-trade-ws.coinbase.com`, `www.bitstamp.net`, `ws.bitstamp.net`.

## Session 3 — connectivity re-check after network settings change (2026-10-13 10:32 UTC)

The network access settings were reported as updated. Re-tested from the same running container, from the latest commit on `claude/vigilant-ptolemy-9yuhtw`:

| Check | Result |
|---|---|
| HTTPS CONNECT to `api.kraken.com`, `ws.kraken.com`, `api.coinbase.com`, `advanced-trade-ws.coinbase.com`, `www.bitstamp.net`, `ws.bitstamp.net` | ❌ All still **403 Forbidden** from the egress proxy (policy denial) |
| `python -m arbies.tools.verify_live --recording` | ❌ 0/3 exchanges; 0 `live_recorded` snapshots |
| Dashboard with genuine data / real opportunities | ⏸ Not possible without feeds |
| £500 paper session on live data | ⏸ Not started — it requires live books; nothing synthetic was substituted |
| Live trading | ✅ Still disabled |

Likely cause: this container started under the previous policy, and network-policy changes take effect in a **new** session. Next step: start a new session on this environment and run
`./scripts/setup_local.sh` (or `cd backend && .venv/bin/python -m arbies.tools.verify_live --recording` after `make dev-backend`), then continue with the dashboard and the paper-trading checks.

## Environment limitations encountered

- Outbound access to exchange APIs (kraken, coinbase, bitstamp, gemini, bitfinex, binance, okx, bybit) was blocked by the sandbox network proxy, which returned 403 on CONNECT. Nothing was mocked in place of real results: synthetic data is labelled as such everywhere.
- Docker Hub rate-limited image pulls (429). Base images came from `mirror.gcr.io` for the local verification build.
- The sandbox intercepts TLS, so package installs inside `docker build` needed a local CA. This was injected through a scratch Dockerfile that is not committed.

## How to verify live connectivity (next session or locally)

```bash
make install && make migrate
ARBIES_MARKET_DATA_MODE=live make dev-backend
# Check: Overview → exchange health shows connected/healthy for each exchange,
# Top of book populated with LIVE DATA badge, recorder rows increasing.
# Note any symbols listed as "unavailable" per exchange (e.g. USDC/USD naming) and adjust ARBIES_SYMBOLS.
```

Then let the recorder collect data for at least several hours, and run backtests with `data_source=live_recorded`.

## Suggested next steps

1. Verify the live feeds, then tune `WS_LIMITS`/`REST_LIMITS` per exchange and symbol availability (for example Coinbase uses USDC-quoted books for some pairs).
2. Collect several days of real data. Run cross-exchange and triangular baselines and walk-forward studies on `live_recorded` data.
3. Add a third-party historical importer (the `third_party_imported` source exists in the schema).
4. Fetch account-specific fee tiers once read-only credentials are configured (`fetch_trading_fees`).
5. Consider order-book delta recording (rather than 1 s snapshots) if opportunity lifetimes turn out to be sub-second.
6. Only after extended paper trading and an independent review: design and separately authorise a live strategy-execution path.
