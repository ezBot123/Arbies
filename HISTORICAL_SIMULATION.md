# Historical investment simulation

The Historical Investment Simulator answers questions like *"If I had started Arbies on 1 January 2025 with
£500 and added £100 every month, how much would I have on 13 October 2026?"*. It does this only as far as
genuine historical data allows. When the data covers part of the period, the simulator says so and reports a
labelled partial result. It never fills the gaps.

Dashboard: **Historical Simulator**. API: `/api/historical/*`. Code: `backend/arbies/historical/`.

## Scenario model

| Item | Default |
|---|---|
| Start / end | 2025-01-01 00:00 UTC → 2026-10-13 23:59:59 UTC |
| Initial deposit | £500 at the start |
| Monthly contribution | £100 at 00:00 UTC on the 1st of each month, from 2025-02-01 to 2026-10-01 (21 deposits) |
| Total personal contributions | £2,600 |
| Leverage | none |
| Profits | reinvested: they stay in the portfolio, and no withdrawals are modelled |
| Allocation of each deposit | split evenly across Kraken, Coinbase and Bitstamp. On each exchange, 50% is held as crypto inventory (BTC, ETH) and 50% as the quote currencies (GBP, USD) |
| Deposit conversion cost | 50 bps (fee plus spread) on every pound converted out of GBP |

Deposits are capital flows and are never counted as profit. Every report splits the change in portfolio value
into four parts, which reconcile exactly:

```
ending value = contributions − conversion costs + trading P&L − rebalancing costs + market/inventory P&L
```

*Trading P&L* comes only from simulated trades. *Market/inventory P&L* is the price movement of the BTC, ETH
and USD that an arbitrage portfolio must hold on each exchange. It is real market risk, but it is not
arbitrage profit.

Return measures:

- **Time-weighted return (TWR)** chains sub-period returns between deposits, so deposits do not count as return.
- **Money-weighted return (XIRR)** is the annual rate that equates the dated deposits with the ending value.
- **Maximum drawdown** is measured on the time-weighted index, so a new deposit never hides a loss.

## How the simulation runs

1. **Order-book events.** Genuine order books are replayed in local-receive-time order:
   `live_recorded` books recorded by Arbies, and `third_party_imported` books, currently Tardis.dev. The
   same deterministic `TradingCore` is used by backtests and paper trading: detection, risk engine, taker
   fills with latency, partial fills, hedge failure and rebalancing. Synthetic data is refused.
2. **No look-ahead.** Decisions only see books received at or before "now", and `TradingCore` raises
   `LookaheadError` otherwise.
3. **Idle periods.** Where no order-book data exists, the bot is idle: no trades, no opportunities, no P&L.
   Monthly rows for those months show trading P&L as **not observed** rather than £0.
4. **Valuation.** Inventory is valued with fresh order-book mids when available. Otherwise it uses the close
   of the most recent completed daily candle on Kraken, falling back to Bitstamp and then Coinbase. A day's
   candle is used only after that day has ended.
5. **Fees.** Fees follow a dated schedule. On a fee-change date, both detection and fills switch to the new
   rate.
6. **Risk limits.** Exposure limits scale with portfolio value: total exposure at most 100%, at most 60% per
   exchange or asset, and a daily loss cap of 5%. This stops limits sized for £500 from blocking a £2,600
   portfolio. Per-trade size limits are unchanged.
7. **Variants.** Strategy sets A (cross-exchange), B (triangular) and C (combined) are each replayed against
   their own portfolio with the same deposits. Variants are independent and deterministic, so they can run in
   parallel worker processes; the results are identical to a sequential run, as `test_parallel_workers_give_identical_results` checks.

## Benchmarks (same contribution dates)

- **A: Cash.** The deposits are held as GBP, with no interest.
- **B: Bitcoin buy and hold.** Each deposit buys BTC at 00:00 UTC on its date, at the previous completed Kraken
  BTC/GBP daily close plus a 10 bps spread allowance, paying the dated Kraken retail taker fee. The result is
  valued at the latest completed close and carries full BTC price risk.
- **C, D, E: Arbies strategies A, B and C.**

## Parameter study (Strategy D)

The combined strategy is run with three presets:

| Preset | Min net profit | Max trade | Latency | Slippage buffer |
|---|---|---|---|---|
| Conservative | 0.50% | £15 | 500 ms | 10 bps |
| Moderate | 0.30% | £25 | 250 ms | 5 bps |
| Aggressive | 0.10% | £50 | 100 ms | 3 bps |

It is also run under the `low` and `high` fee schedules. The observed minutes are split chronologically, with
60% for training and 40% for testing. The preset with the best *training-window* trading P&L is chosen, with
ties going to the more conservative preset. Its *test-window* result is then reported. Test results are never
used for selection, and the agents cannot see them.

## Historical data: what exists and what was used

| Source | What it provides | 2025-01-01 → 2026-10-13 | Used for |
|---|---|---|---|
| **Tardis.dev free samples** | L2 order books (`incremental_book_L2`, `book_snapshot_5/25`, `quotes`, `trades`) with exchange and capture timestamps | **Only the 1st of each month** without an API key. Any other day returns HTTP 401 and needs a paid subscription. | Order-book replay on the 22 first-of-month days |
| Tardis.dev paid | The same data for every day | Available, but paid. **Not purchased.** | — |
| Kraken public API | Daily OHLC (last 720 bars, back to 23 Oct 2024), full tick trade history, current book only | Daily candles and trades are complete | Daily reference prices (valuation, benchmark), GBP/USD |
| Bitstamp public API | 1-minute and daily OHLC for any historical range, current book only | Complete | 1-minute candle screening, cross-check prices |
| Coinbase public API (v3) | 1-minute and daily OHLC (minutes without trades are omitted), recent trades and current book only | Complete | 1-minute candle screening, cross-check prices |
| Arbies live recorder | L2 books, 1 s snapshots | 2026-10-13 only | Order-book replay |

None of the exchanges offers historical order books through its public API. Daily or minute candles are
**never** used to simulate arbitrage trades. They only value inventory and power the benchmark and the
screening.

Tardis symbol ids: Kraken spot uses `XBT-USD`, `XBT-GBP` and `ETH-XBT` up to 2026-07-09, and
`BTC-USD`, `BTC-GBP` and `ETH-BTC` from 2026-07-10. Bitstamp uses `BTCUSD` and similar; Coinbase uses
`BTC-USD`. Tardis rejects byte-range requests (403), so files are downloaded whole.

Import command (free files only; each raw file is deleted after import, and its URL and SHA-256 are kept in
`historical_datasets`):

```bash
cd backend && .venv/bin/python -m arbies.tools.fetch_tardis --start 2025-01 --end 2026-10 --exchanges coinbase bitstamp kraken
.venv/bin/python -m arbies.tools.fetch_reference --interval 1d --start 2024-12-01 --end 2026-10-14 \
  --markets kraken:BTC/GBP kraken:BTC/USD kraken:ETH/GBP kraken:ETH/USD kraken:GBP/USD kraken:ETH/BTC
```

### Importer guarantees

- **Formats:** Tardis `incremental_book_L2` (full L2 reconstruction, with snapshot resets),
  `book_snapshot_N` and `quotes`. Trades files are rejected because they are not order books.
- **Timestamps:** the exchange timestamp is kept as `exchange_ts`; the provider capture time is used as
  `received_ts`, the time a trading system could have known about the update.
- **Labelling:** every row is `source='third_party_imported'` with a `dataset_id` that points at its
  provenance record (provider, URL, file name, SHA-256, size, validation report).
- **Validation:** invalid rows, exact duplicate rows, out-of-order local or exchange timestamps, gaps over 30 s,
  crossed books and empty sides. Crossed books are flagged, never repaired.
- **Resumable:** the line offset, reconstructed book state and pending message group are committed atomically
  with each batch. An interrupted import resumes to an identical result, as tested.
- **Idempotent:** an identical file (same SHA-256, interval and depth) is never imported twice.
- **Sampling:** books are sampled at 1 s by default. Rows that sampling would drop are not fully parsed, but
  their timestamps are still validated.
- **Retention:** data retention never deletes imported datasets.
- **Confinement:** the import API only reads files inside `ARBIES_HISTORICAL_DATA_DIR`.

## Fee research (retail tiers a £500 account could reach)

| Exchange | Period | Maker / taker | Verification | Notes |
|---|---|---|---|---|
| Kraken | 2025-01-01 → 2026-07-08 | 0.25% / 0.40% | secondary sources | Kraken's current page only shows the new table |
| Kraken | from 2026-07-09 | 0.40% / 0.80% | Kraken blog and fee page | Cross-platform fee tiers |
| Coinbase Advanced | whole period | 0.60% / 1.20% (base) | secondary sources | Sources conflict with 0.40% / 0.60% ($0–10k), which is used as `low`. A 16 Sep 2026 "lower" schedule was not published. |
| Bitstamp | whole period | 0.30% / 0.40% (base) | secondary sources | An undated claim of 0% under $1k is used in `low` only |

Schedules: `base` takes the conservative value wherever sources conflict, `low` uses the lower figures, and
`high` is `base` × 1.25. Low fees are never silently substituted. Each entry carries its source and
verification status, which the dashboard shows. Account-specific tiers need credentials and are not modelled.

## Candle screening (whole period, indicator only)

For each minute in which both Bitstamp and Coinbase traded a pair, the two 1-minute closes are compared
against the dated round-trip taker fees. A close is the last trade in the minute, not an executable quote,
and the two closes can be up to 59 s apart. Candles also carry no depth. The screening is therefore only an
indicator of whether fee-beating price gaps existed. It produces no P&L, and minutes without trades are
skipped, never filled.

## Validation (automated tests)

| # | Requirement | Test |
|---|---|---|
| 1 | Recurring deposit calculations | `test_scenario_schedule_has_21_monthly_deposits_totalling_2600`, `test_deposits_arrive_on_schedule_and_are_not_profit` |
| 2 | Deposits are not profit | `test_deposits_arrive_on_schedule_and_are_not_profit`, `test_twr_removes_deposit_effect` |
| 3 | Cash and crypto balances reconcile | `test_trading_reconciles_and_fees_reduce_value` (accounting identity, monthly sums) |
| 4 | Fees reduce value correctly | `test_trading_reconciles_and_fees_reduce_value`, `test_fee_schedule_change_is_applied_on_its_date` |
| 5 | Inventory never negative | `test_inventory_never_negative` |
| 6 | Reproducible | `test_repeated_runs_are_identical`, `test_parallel_workers_give_identical_results`, service hash test |
| 7 | Chronological processing | `test_out_of_order_events_are_rejected` |
| 8 | No future data | `test_reference_prices_never_look_ahead`, plus `TradingCore`'s `LookaheadError` |
| 9 | Equivalent contribution schedules | `test_benchmarks_use_identical_contribution_schedule` |
| 10 | Incomplete data is not treated as continuous | `test_sparse_data_is_reported_as_partial` |
| 11 | Charts match database records | `test_persisted_results_views_and_csv_agree` |
| 12 | No fictional trades in gaps | `test_trading_reconciles_and_fees_reduce_value` (trades only inside the data window), `test_simulator_refuses_synthetic_sources` |

Importer: `tests/test_historical_importer.py`. Agent sealing: `test_historical_agent_reads_results_but_test_window_is_sealed`.

## Limitations

- **Free order-book data covers 22 sampled days.** A sampled day says nothing about the days around it, and
  results from those days are never scaled up to the whole period.
- **Fills are taker-only by default.** Maker fills are never assumed; they can be configured, with an explicit
  fill probability.
- **Latency** is a fixed configured value plus jitter, not a measured historical value.
- **Withdrawals** use static withdrawal fees and transfer delays for rebalancing.
- **Not modelled:** interest, staking, tax, bank deposit fees and account-level fee discounts.
