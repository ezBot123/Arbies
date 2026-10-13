# Arbies — progress record

This file is kept up to date so development can resume in a later session.

## Milestones

| # | Milestone | State | Notes |
|---|---|---|---|
| 1 | Foundation | ✅ Done | FastAPI backend, React dashboard, PostgreSQL + Alembic, Docker / Compose, config, structured logging, Makefile, CI workflow, tests |
| 2 | Market data | ✅ Done · live verified | CCXT Pro WebSocket adapter with REST fallback, reconnect/backoff, crossed-book resync, clock sync, rules and fees, health monitoring, recorder, live stream to the dashboard. Verified on genuine Kraken, Coinbase and Bitstamp public feeds (Session 5). |
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
| ≥ 2 eligible exchange market-data connections work | ✅ | 3/3: Kraken, Coinbase and Bitstamp, over WebSocket, with no API keys (Session 5). Kraken's WebSocket stalls about once a minute in the cloud container; see the known issues. |
| Live order books displayed | ✅ | Dashboard top of book matched `/api/orderbooks` row for row with the LIVE DATA badge (Playwright, Session 5) |
| Cross-exchange opportunities detected | ✅ | Unit tests with exact arithmetic; live scanner on synthetic feeds |
| Triangular opportunities detected | ✅ | Unit tests; live scanner on synthetic feeds |
| Realistic fee and liquidity estimates | ✅ | Depth-walked VWAP, per-exchange fees, costs itemised |
| Historical data recorded and persisted | ✅ | Recorder writes throttled snapshots; recorder → loader round-trip test |
| Backtests can be executed | ✅ | Via API, service and UI (Playwright-driven run: 64,800 events, 11 trades) |
| Backtest results reproducible | ✅ | Identical result hash across runs (engine and service tests) |
| Paper trading works against live market data | ✅ | £500 session #3 on `live_recorded` books; survives a restart as paused and resumes (Session 5) |
| Realistic portfolio accounting | ✅ | Per-exchange balances, atomic trades, no negative balances, fees, transfers in transit, frozen-mark P&L |
| AI research agents function | ✅ deterministic · ⚠️ LLM | All six run on real system data. The LLM loop was tested with a scripted fake client only; no API key was available. |
| Strategy comparisons work | ✅ | Compare tab and API; reports by strategy and exchange |
| Risk controls tested | ✅ | 20+ risk, gate and permission tests |
| Dashboard displays real application data | ✅ | Every page screenshotted against a running backend; no hard-coded metrics |
| Tests run successfully | ✅ | 89 passed (Session 5; previously 81 on SQLite and on PostgreSQL 16) |
| Documentation complete | ✅ | README, ARCHITECTURE, DEVELOPMENT, BACKTESTING, STRATEGIES, RISK_MANAGEMENT, SECURITY, DEPLOYMENT |
| Live trading remains disabled | ✅ | Env flag defaults to false; arming refused in tests; no strategy → live path |

**Overall:** every item above is verified. Live-data connectivity was verified in Session 5. Open items, none of which block paper research: (1) fee rates are CCXT defaults and may not match current exchange schedules; (2) Kraken's WebSocket stalls periodically in the cloud container. See "Session 5, known issues".

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

## Session 4 — new cloud session after network settings update (2026-10-13 10:42 UTC)

Fresh container, started from `293b917` on `claude/vigilant-ptolemy-9yuhtw`. So the network policy was the one in force when this session started.

| Check | Result |
|---|---|
| HTTPS CONNECT to `api.kraken.com`, `ws.kraken.com`, `api.coinbase.com`, `advanced-trade-ws.coinbase.com`, `www.bitstamp.net`, `ws.bitstamp.net` | ❌ All still **403 Forbidden** from the egress proxy (`CONNECT tunnel failed, response 403`) |
| Control: `pypi.org`, `api.github.com` through the same proxy | ✅ 200 (the proxy works; only the exchange hosts are denied) |
| `python -m arbies.tools.verify_live` | ❌ `Exchange feeds: 0/3 verified`, `OVERALL: FAIL` (`load_markets: ExchangeNotAvailable` on all three) |
| Two exchanges on real public data / real order-book updates | ⏸ Not possible |
| Real snapshots recorded in PostgreSQL | ⏸ Not possible (no feeds) |
| Dashboard with genuine live prices | ⏸ Not possible |
| £500 paper session, fee calculations, simulated trades, portfolio accounting on live data | ⏸ Not started. It requires live books, and nothing synthetic was substituted |
| Live trading | ✅ Still disabled |

Conclusion: the session restart did not help. The environment's network policy still does not allow the exchange hosts. Retries stopped here, and no attempt was made to work around the proxy.
To unblock: in the cloud environment settings, under Network access, either choose a broader access level or add the six hosts above under Allowed domains (leave "Allow package managers" ticked). See https://code.claude.com/docs/en/cloud-environments#network-access. Then start a new session and run `cd backend && .venv/bin/python -m arbies.tools.verify_live`.

### Session 4 update — 10:45 UTC re-check

After a further settings change, the exchange hosts now pass the egress proxy. `curl` returned real responses: kraken 404/200, coinbase 301/200, bitstamp 200/426, all from the exchanges themselves, with no proxy 403s.

`verify_live` still reports `0/3` (`ExchangeNotAvailable`). This is now a **client-side** issue: ccxt's async REST client uses aiohttp, and aiohttp ignores `HTTPS_PROXY` unless `trust_env` is set. The WebSocket client likewise needs the proxy passed explicitly (`wssProxy`), so the Python process is trying to connect directly and fails.
Proposed fix, not yet applied or tested: build every ccxt client (`market_data/ccxt_adapter.py`, `tools/verify_live.py`) with `aiohttp_trust_env=True` and, when `HTTPS_PROXY` is set, `wssProxy=$HTTPS_PROXY`. The test run of this change was held for user approval by the session's permission system. Nothing synthetic was substituted, and live trading is still disabled.

## Session 5 — live-data validation (2026-10-13 10:45–11:10 UTC)

The exchange hosts became reachable after the network settings were updated. The Python clients still failed, because CCXT's aiohttp transport ignores `HTTPS_PROXY` unless told otherwise. It also pins certifi rather than `SSL_CERT_FILE`. The proxy support follows the container proxy's own README ("aiohttp (pass trust_env=True) … prefer the tool's own proxy option"). It only reads the standard environment variables, routes through the permitted proxy, and leaves TLS verification on.

### Code changes

- **`market_data/ccxt_config.py` (new):** used by every CCXT client (market-data adapter, `verify_live`, live-account clients). It sets `aiohttp_trust_env=True`, sets `wssProxy` from `HTTPS_PROXY`, and sets `cafile` from `SSL_CERT_FILE`. It does nothing when those are unset. Covered by 4 tests.
- **Fix: `markets.fee_source` was `varchar(16)`,** but the adapter writes a 24-character label. On live data this made the recorder's persist loop fail every 2 s, so no snapshots were saved. The column is now `varchar(32)` (migration `7b1e2c4d9a10`; `alembic check` clean).
- **Fix: crossed WebSocket books.** Bitstamp's CCXT diff-book drifts (missed deletions). In the first 3 minutes, 21% of Bitstamp snapshots were crossed, and BTC/USD stayed crossed continuously. A WebSocket book that stays crossed for more than 5 s is now resubscribed (`unWatchOrderBook`). After 3 resyncs in 120 s that symbol moves to REST polling. Health gains a `resyncs` counter, and `resync` events show on the dashboard. Crossed Bitstamp snapshots fell to 2.7%: these are the detection window, are flagged in `quality.crossed`, and both scanners skip them. Covered by 3 tests.
- **Fix: paper sessions did not survive a graceful restart.** `Runtime.stop()` called `paper.stop()`, which marked the session `stopped`, so `restore()` never found it. Shutdown now calls `paper.suspend()`, which flushes trades and saves the session as `paused`. Covered by a regression test, and verified live: session 3 came back as paused and resumed as the same session.
- **Fee provenance label:** `exchange_public_schedule` became `ccxt_default_schedule`. Kraken's public API no longer publishes fee tiers, so these rates come from CCXT's bundled table, not from the exchange. ARCHITECTURE.md is updated to match.

### Verification results (all on genuine public data; nothing synthetic substituted)

| Check | Result |
|---|---|
| `verify_live` | ✅ **3/3 PASS**. Kraken, Coinbase and Bitstamp each loaded markets and returned REST books for 6/5/6 symbols (Coinbase does not list USDC/USD). BTC/USD agreed across venues at about $83,220. |
| Real order-book updates | ✅ WebSocket BTC/USD in 10 s: Kraken 172–181, Coinbase 107–120, Bitstamp 19–20 updates |
| Backend in live mode | ✅ 3/3 exchanges `connected`/`healthy` over WebSocket, 17 order books |
| Historical snapshots in PostgreSQL | ✅ 11,914 `orderbook_snapshots` rows, all `source='live_recorded'`, covering 3 exchanges and 17 markets (10:51–11:09 UTC, about 12 rows/s). `verify_live --recording` PASS. Recorded `exchange_ts` and `received_ts` agree to within about 20 ms on Kraken and Coinbase; Bitstamp exchange timestamps have whole-second resolution. |
| Dashboard shows genuine prices | ✅ Playwright login. The Overview top-of-book table matched `/api/orderbooks` for all 17 markets, with the LIVE DATA and PAPER MODE · SIMULATED badges and 3/3 healthy exchanges. The Paper and Portfolio pages render real balances. |
| £500 paper session | ✅ Session 3, `data_source=live_recorded`, initial value £499.9996. The split is 12 × £41.67 across 3 exchanges × {GBP, USD, BTC, ETH}, valued at live mids (BTC/GBP about £62,803; implied GBP/USD 1.3240). |
| Paper trades | ✅ **0 trades, which is correct.** About 8,700 evaluations and 127 detected routes, with 0 eligible. Rejections: fees_exceed_profit, below_min_order, insufficient_liquidity, stale_data. Real cross-venue gaps were at most about 3 bp, against 0.66–1.6% round-trip taker fees. P&L of about ±£0.06 is inventory revaluation only. |
| Fee and cost calculation | ✅ The opportunity `bitstamp→kraken ETH/USD` was recomputed independently, and every component matched to the last digit: buy $33.09913532, sell $33.10963758, fees $0.21848160 (0.40% + 0.26%), slippage, latency, rebalancing and leg-failure costs, and net −$0.29073453 (−0.878%). |
| Simulated trades and accounting | ✅ Backtest #2 is a **mechanics check**: real recorded books, 1 bp *test* fees and zero modelled costs, labelled in the run name; it is not a strategy. It made 20 trades with 20 successful cycles. All 40 legs: quantity × VWAP = quote amount, and fee = 1 bp. Σ trade P&L = realised P&L (−£0.00133453); Σ fees = fees paid (£0.08995717 = 1 bp × £899.57 volume). Initial balances + fills − fees = final balances, exactly. 40/40 fill prices equal the recorded real top of book at fill time. Backtest #1 used real fee rates and the threshold at −2%: 0 trades (fees always exceed spread). |
| Tests / lint | ✅ 89 passed; ruff clean |
| Live trading | ✅ Disabled: `ARBIES_LIVE_TRADING_ENABLED=false`, gate `env_enabled=false`, `armed=false` |

### Known issues (open)

1. **Fee rates may be out of date.** CCXT's bundled base-tier rates are Kraken 0.26% / 0.16%, Coinbase 1.20% / 0.60% and Bitstamp 0.40% / 0.40% (taker / maker). Third-party sources disagree about the current schedules, and some report that Kraken's entry tier changed in July 2026 to as much as 0.40% maker / 0.80% taker. This could not be confirmed from an official fee table here. **Check each exchange's fee page or your account tier, and set `fee_overrides`.** Higher real fees make every result more conservative; none of today's results depend on these fees being low.
2. **Kraken WebSocket stalls in the cloud container.** About every 45–60 s, the Kraken v2 stream delivers nothing, neither data nor pongs, for about 12 s. CCXT then raises "ping-pong keepalive missing", and the adapter reconnects within 1–2 s. This reproduces in a standalone script with no app code, and asyncio debug mode showed no event-loop blocking. Coinbase and Bitstamp are unaffected, and the proxy recorded no relay failures. It is therefore probably specific to this network path. The staleness gate keeps those books out of trading. **Check on a local machine**: if it happens there too, consider REST fallback for Kraken or Kraken's v1 endpoint.
3. Bitstamp ETH/USD and BTC/USD still re-drift occasionally. The resync and REST fallback contain it (5 resyncs and 1 REST fallback in 4 minutes).
4. Quiet markets (Kraken USDC/USD, ETH/BTC) can exceed `max_data_age_ms=5000` between real updates and are then treated as stale. This is safe and conservative.

## Environment limitations encountered

- Sessions 1–4: outbound access to exchange APIs was blocked by the sandbox network proxy, which returned 403 on CONNECT. From Session 5, Kraken, Coinbase and Bitstamp are allowed. Nothing was mocked in place of real results: synthetic data is labelled as such everywhere.
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

1. Confirm current fee schedules and set `fee_overrides`, then check the Kraken WebSocket stall on a local machine (Session 5 known issues).
2. Collect several days of real data. Run cross-exchange and triangular baselines and walk-forward studies on `live_recorded` data.
3. Add a third-party historical importer (the `third_party_imported` source exists in the schema).
4. Fetch account-specific fee tiers once read-only credentials are configured (`fetch_trading_fees`).
5. Consider order-book delta recording (rather than 1 s snapshots) if opportunity lifetimes turn out to be sub-second.
6. Only after extended paper trading and an independent review: design and separately authorise a live strategy-execution path.
