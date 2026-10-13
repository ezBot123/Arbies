# Backtesting

Backtesting is a core part of Arbies. The engine (`backend/arbies/backtest/engine.py`) replays recorded order-book snapshots event by event. It runs the same `TradingCore` that the paper trader uses.

## Data

| Source label | Meaning |
|---|---|
| `live_recorded` | Recorded by Arbies from real exchange feeds |
| `third_party_imported` | Imported historical data. The schema supports it; no importer is shipped yet. |
| `synthetic` | Generated test data, labelled everywhere and never a real observation |

Every backtest names exactly one source, and the query filters on it, so sources are never mixed silently. If no data exists for the chosen source, exchanges, pairs and period, the run fails with an explanation rather than producing numbers.

Snapshots carry:

- the top N levels (default 20) on each side;
- `received_ts` (local UTC);
- the exchange timestamp, when the exchange provides one;
- a sequence number, when the exchange provides one;
- quality flags: crossed book, gap since the previous snapshot, empty side.

`find_gaps` reports gaps longer than 30 s, and these appear in the run's warnings.

**Dataset identity.** A dataset id is a hash of the source, exchanges, symbols, window, row count and id range. It is stored with each run, together with the full config, the code revision (`git rev-parse` or `ARBIES_CODE_REVISION`), the seed and a SHA-256 **result hash**.

**Synthetic datasets** can be generated from the UI or with `POST /api/data/synthetic`. They are capped at 400,000 snapshots.

## Replay rules (integrity)

1. **Chronological order by `received_ts`.** This is when Arbies would actually have known about each update, and it preserves the observed timing between exchanges. Out-of-order input raises an error.
2. **No future leakage.** `TradingCore.decide` raises `LookaheadError` if any book it is given has `received_ts` after the decision time. Tests cover this guard, and they also check that a dislocation appearing at t=60 s produces no trade decided earlier.
3. **Execution happens after latency, against the book at that time.** Orders are submitted at decision time and become due at decision time plus simulated latency (base + seeded jitter). They are filled against whatever book state exists when the replay reaches that moment, never against the book that triggered the decision. A test checks that an opportunity which vanishes within the latency window produces a failed or hedged trade, not a fill at the stale price.
4. **Balances are enforced.** `Portfolio.apply_trade` is atomic and raises if a spend would exceed the available balance. A leg like that is recorded as `rejected`.
5. **Determinism.** Latency jitter and maker fills use `random.Random(seed)`. The same config, data and seed give the same result hash, and this is tested end to end through the service and the database.

## Fill model

| Setting | Behaviour |
|---|---|
| Taker (default) | Immediate-or-cancel limit orders at the estimated VWAP ± tolerance (`max_slippage_pct`, default 0.20%). They consume book levels up to the limit and can fill partially. |
| Maker | A resting order at the limit price that fills completely with probability `maker_fill_probability` within the window, or not at all. This is a deliberately crude queue model; treat maker results with caution. |
| Lot size / minimums | Amounts are rounded down to the exchange's lot step. Orders below minimum amount or cost are rejected. Lot rounding is not counted as a partial fill. |
| Cross-exchange | Both legs execute at the same moment. If the filled quantities differ, `unwind` (the default) reverses the excess with an unlimited taker order after another latency window. `hold` keeps it as inventory exposure instead. |
| Triangular | Legs execute in sequence, one latency window apart, and each leg spends what the previous one actually received. A failed or partial leg leaves the remainder in an intermediate currency, which is unwound to the start currency through a direct market when one exists. Dust below the minimum order size is kept and noted. |

## Inventory and rebalancing

The starting allocation is configurable per exchange and asset. By default, capital is split evenly across exchanges; on each exchange, half is spread across the fiat quote currencies in use and half across the base assets.

When rebalancing is enabled, a check runs every `check_interval_s`. If one exchange holds more than `threshold_pct` of an asset, a transfer moves it back towards `target_pct`. The transfer pays a flat withdrawal fee plus `transfer_fee_bps`, and the funds are unavailable while in transit (`transfer_delay_s`). No transfer is made when the fee would exceed 10% of the amount. The withdrawal fees are placeholders; verify them for your exchanges.

## Configurable inputs

- Starting capital and allocation; exchanges, pairs and period; data source.
- Strategies: cross-exchange and/or triangular.
- Strategy parameters: minimum profit threshold, maximum trade size, fee overrides, slippage buffer, latency and latency cost, rebalancing and FX costs, leg-failure probability and loss.
- Fill model: order type, maker fill probability, limit tolerance, hedge policy.
- Risk limits: the full set described in [RISK_MANAGEMENT.md](RISK_MANAGEMENT.md).
- Rebalancing rules, decision interval and seed.

## Metrics

| Group | Metrics |
|---|---|
| Value and returns | Initial / final value, total P&L, net return |
| Drawdown and risk | Max drawdown (£ and %), max and final inventory exposure |
| Activity | Trading volume, opportunities detected (positive-spread episodes), eligible (qualified episodes), attempted, successful / partial / failed cycles, fill ratio, average net margin |
| Costs | Fees, slippage versus estimate, failed-hedge losses, rebalancing costs and count |
| Breakdowns | P&L by exchange (value change of each fill at marks), by pair or route, by day, by UTC hour |
| Diagnostics | Rejection-reason counts (strategy filters) and risk-engine rejection counts |
| Equity curve | Sampled every `equity_sample_interval_s` |
| Data | Warnings and data gaps |

**P&L decomposition.**

- *Trade P&L* is the portfolio value change across each execution, valued at mid prices frozen at decision time. It isolates execution quality.
- *Inventory / market P&L* is the rest of the total change. It comes from price moves on held inventory, which in cross-exchange arbitrage can dominate.

**Sharpe ratio.** It is computed from daily close-to-close equity returns, with risk-free rate 0 and a 365-day year. It needs at least 3 daily returns; otherwise it is reported as n/a with the reason, so short backtests show n/a.

## Walk-forward validation

`WalkForwardService` splits the period into training / validation / test windows (60/20/20 by default):

1. Each parameter combination in the grid (at most 64) is backtested on the **training** window.
2. The top-k by the objective are re-run on the **validation** window, and the best validation result is **locked**.
3. The out-of-sample **test** window is never loaded during optimisation. It can be evaluated only after locking, exactly once, by the user.
4. A locked study cannot be re-optimised; create a new study instead. AI agents can run optimisation, but they have no tool to run the test, and test-segment runs are hidden from their tools.

Gridable parameters: `min_net_profit_pct`, `slippage_buffer_bps`, `latency_cost_bps`, `rebalancing_cost_bps`, `leg_failure_prob`, `leg_failure_loss_bps`, `max_trade_gbp`.

Objectives: total P&L, net return, Sharpe, successful cycles.

## Comparing runs

Tick runs in the Backtesting Lab's Runs tab and open the Compare tab, or call `GET /api/backtests/compare?ids=1,2,3`. You get overlaid equity curves (plotted against elapsed time) and a metric table. All runs are stored for later review, and their trades can be exported as CSV.

## Known limitations

- Snapshots are taken at most once per second per market by default. Faster micro-structure, including order-book updates between snapshots, is not captured, so realised opportunity lifetimes may be shorter than modelled.
- Queue position, hidden liquidity and your own market impact on other venues are not modelled.
- The maker fill model is probabilistic, not a queue simulation.
- FX conversion for quote equivalents (for example USD versus USDC) uses a flat cost in basis points, not a recorded FX rate.
- Results on synthetic data only test the engine. They say nothing about profitability.
