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

## Results: £500 + £100/month, 1 Jan 2025 → 13 Oct 2026 (simulation #4)

Run on 2026-10-13. Result hash `006aa79edda2038ae2a3dbf5342684bcff58c9b2af3bfcc759215ee9beed2955`. Every
figure below comes from the stored simulation record. In the dashboard: Historical Simulator → #4.

### A. Historical investment result: partial, not a full-period answer

| | Value |
|---|---:|
| Initial investment | £500.00 |
| Monthly contributions (21 × £100, 1 Feb 2025 → 1 Oct 2026) | £2,100.00 |
| **Total personal contributions** | **£2,600.00** |
| **Trading profit / loss (observed periods only)** | **£0.00** (no trades) |
| Market / inventory P&L (BTC, ETH and USD held on the exchanges) | −£69.94 |
| Deposit conversion costs | −£9.75 |
| **Ending portfolio value** (at 12 Oct 2026 daily closes) | **£2,520.31** |
| Time-weighted return (excludes deposits) | −11.51% |
| Money-weighted return (XIRR, annual) | −2.95% |
| Maximum drawdown (time-weighted index) | 35.08% |
| Best / worst net position (value − contributions) | +£256.43 (7 Oct 2025) / −£440.30 (1 Jul 2026) |

Trading could be observed for **532.8 of 15,624 hours (3.41%)**: the 22 free first-of-month Tardis days plus
about 5 hours of Arbies' own live recording. The £2,520.31 is therefore *contributions plus the genuine price
movement of the inventory, plus zero trading P&L where trading was observable*. It does **not** say what Arbies
would have earned on the other 629 days, and nothing has been extrapolated to them.

### B. Monthly performance (combined strategy C, moderate preset, base fees)

Every month's trading P&L comes from its one observed day, the 1st of the month. October 2026 also includes
13 October.

| Month | Personal deposits | Trading P&L (observed day) | Fees | Market P&L | Ending portfolio |
|---|---:|---:|---:|---:|---:|
| January 2025 | £500 | £0.00 | £0.00 | +£14.57 | £512.69 |
| February 2025 | £100 | £0.00 | £0.00 | −£82.16 | £530.16 |
| March 2025 | £100 | £0.00 | £0.00 | −£37.89 | £591.89 |
| April 2025 | £100 | £0.00 | £0.00 | +£7.30 | £698.82 |
| May 2025 | £100 | £0.00 | £0.00 | +£76.82 | £875.26 |
| June 2025 | £100 | £0.00 | £0.00 | −£12.65 | £962.23 |
| July 2025 | £100 | £0.00 | £0.00 | +£177.17 | £1,239.03 |
| August 2025 | £100 | £0.00 | £0.00 | +£28.56 | £1,367.22 |
| September 2025 | £100 | £0.00 | £0.00 | −£4.00 | £1,462.84 |
| October 2025 | £100 | £0.00 | £0.00 | −£24.36 | £1,538.11 |
| November 2025 | £100 | £0.00 | £0.00 | −£186.94 | £1,450.79 |
| December 2025 | £100 | £0.00 | £0.00 | −£35.04 | £1,515.37 |
| January 2026 | £100 | £0.00 | £0.00 | −£125.60 | £1,489.39 |
| February 2026 | £100 | £0.00 | £0.00 | −£107.24 | £1,481.78 |
| March 2026 | £100 | £0.00 | £0.00 | +£47.78 | £1,629.19 |
| April 2026 | £100 | £0.00 | £0.00 | +£35.89 | £1,764.70 |
| May 2026 | £100 | £0.00 | £0.00 | −£47.17 | £1,817.16 |
| June 2026 | £100 | £0.00 | £0.00 | −£157.09 | £1,759.70 |
| July 2026 | £100 | £0.00 | £0.00 | +£67.42 | £1,926.75 |
| August 2026 | £100 | £0.00 | £0.00 | +£234.38 | £2,260.76 |
| September 2026 | £100 | £0.00 | £0.00 | +£125.42 | £2,485.80 |
| October 2026 (to 13th) | £100 | £0.00 | £0.00 | −£65.11 | £2,520.31 |

Each month also had £0.375 of deposit conversion cost (£1.875 in January). Market P&L is the revaluation of
inventory at genuine Kraken daily closes. The figures reconcile exactly: value change −£79.69 = −£9.75
conversion − £69.94 market + £0.00 trading.

### C. Strategy comparison (observed periods only)

| Strategy | Evaluations | Price-gap episodes | Passed all checks | Trades | Net trading P&L |
|---|---:|---:|---:|---:|---:|
| A: cross-exchange | 27.3M | 752,214 | 0 | 0 | £0.00 |
| B: triangular | 21.8M | 248,224 | 0 | 0 | £0.00 |
| C: combined (shared capital) | 49.1M | 1,000,438 | 0 | 0 | £0.00 |

Why the combined strategy rejected every episode:

| Reason | Episodes |
|---|---:|
| Fees exceed profit | 882,613 |
| Below the exchange minimum order | 126,647 |
| Insufficient depth | 117,584 |
| Stale data | 6,996 |
| Exchange unavailable | 2,675 |
| Insufficient inventory | 3 |
| Profitable but below the 0.30% threshold | 3 |

No strategy outperformed another.

**Strategy D, parameters and fees.** None of these variants traded:

| Variant | Trades |
|---|---:|
| Conservative (0.50%, £15) | 0 |
| Moderate (0.30%, £25) | 0 |
| Aggressive (0.10%, £50, 100 ms) | 0 |
| `low` fees (Coinbase 0.60% taker, Bitstamp 0%) | 0 |
| `high` fees (+25%) | 0 |

The train/test split (train up to 2026-02-01 07:41 UTC) selected *conservative* only by the tie rule, and its
test-window P&L was £0.00. Because nothing traded, the study cannot tell the presets apart.

### D. Benchmarks (same contribution dates)

| Portfolio | Ending value | Net gain | XIRR | Market risk |
|---|---:|---:|---:|---|
| A: cash, no interest | £2,600.00 | £0.00 | 0.00% | none |
| B: Bitcoin buy and hold (0.03951985 BTC, £11.60 fees) | £2,447.13 | −£152.87 | −5.69% | full BTC exposure (TWR −17.81%) |
| C, D, E: Arbies A / B / C | £2,520.31 | −£79.69 | −2.95% | inventory exposure (TWR −11.51%, max drawdown 35%) |

The Bitcoin benchmark was recomputed independently from the stored Kraken closes and matched to 8 decimal
places. Arbies did better than Bitcoin only because half its money sat in GBP and USD, not because of trading.
Arbitrage is meant to be market-neutral per trade, but the inventory it must hold on every exchange is not.

### E. Data quality and coverage

| Data | Coverage | Quality |
|---|---|---|
| Tardis `book_snapshot_5`: Kraken, Coinbase and Bitstamp × BTC/USD, ETH/USD, BTC/GBP, ETH/GBP, ETH/BTC | **22 days**: the 1st of every month from Jan 2025 to Oct 2026. 330 of 330 files, 18.6 GB, 1.087 billion rows → 20.96M one-second snapshots | 0 crossed books, 0 out-of-order rows, 0 invalid rows, 0 duplicates. 295 gaps over 30 s (Kraken 166, Bitstamp 97, Coinbase 32), mostly on the quieter GBP and ETH/BTC books |
| Arbies live recording | 13 Oct 2026, about 5 h | Bitstamp crossed-book episodes handled by resync, as described in Session 5 |
| Daily reference candles (Kraken, plus Bitstamp and Coinbase cross-checks) | Every day, 1 Dec 2024 → 13 Oct 2026 | Complete |
| 1-minute candles (Bitstamp, Coinbase: BTC/USD, BTC/GBP, ETH/USD) | Every minute of the period (Coinbase omits minutes without trades) | Complete |
| **Missing** | **629 of 651 days of order books.** Tardis serves days other than the 1st only to paying customers, which would need your approval. Kraken spot is listed under `XBT-*` ids before 10 Jul 2026. | — |

**Candle screening across all 651 days.** This is an indicator only: last-trade closes up to 59 s apart, with
no depth. It shows whether the missing days probably held opportunities.

| Bitstamp vs Coinbase | Minutes compared | Median gap | 99th percentile | Minutes above base / low fees |
|---|---:|---:|---:|---:|
| BTC/USD | 928,421 | 0.008% | 0.051% | 11 / 28 |
| BTC/GBP | 197,348 | 0.029% | 0.262% | 17 / 271 |
| ETH/USD | 900,751 | 0.011% | 0.097% | 14 / 39 |

The fee-beating minutes cluster in a few market crashes. The largest were 21:17–21:35 UTC on 10 Oct 2025
(BTC up to 4.3%) and 02:23–02:25 UTC on 3 Feb 2025 (ETH up to 5.9%). Those moments fall outside the free
order-book days, and they are when real books are thinnest and slowest. Over 21 months, the total excess over
base fees came to about 44 percentage points across all three pairs. At £25 per trade, that would be on the
order of £10, *if* every candidate had been executable, which candles cannot show. This is not a profit
estimate.

**Reliability:**
- The "no trading profit on observed days" finding rests on 21 million genuine order books and holds under
  every parameter and fee scenario.
- The full-period candle screening points the same way.
- The fee schedules partly rest on secondary sources (shown per entry). The `low` scenario, with the most
  favourable figures found, still produced no trades.

### F. What it would take to answer the question completely

1. **Continuous Level 2 data** for all 651 days, for example a paid Tardis subscription. This is not purchased,
   and it needs your explicit approval and a cost check. Alternatively, buy only the days the candle screening
   flags, such as 10 Oct 2025 and 3 Feb 2025, to test the best-case moments cheaply.
2. **Account-specific fee tiers** confirmed from statements.
3. **Measured latency** on your own infrastructure. Today's model uses a configured 100–500 ms.

## Limitations

- **Free order-book data covers 22 sampled days.** A sampled day says nothing about the days around it, and
  results from those days are never scaled up to the whole period.
- **Fills are taker-only by default.** Maker fills are never assumed; they can be configured, with an explicit
  fill probability.
- **Latency** is a fixed configured value plus jitter, not a measured historical value.
- **Withdrawals** use static withdrawal fees and transfer delays for rebalancing.
- **Not modelled:** interest, staking, tax, bank deposit fees and account-level fee discounts.
