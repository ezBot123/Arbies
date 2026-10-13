# Strategies

Both detectors are pure functions in `backend/arbies/arbitrage/`. They take order books, market rules, available balances, exchange health and `StrategyParams`, and return `Opportunity` objects with a full cost breakdown and machine-readable rejection reasons. They never use mid prices for profitability. Every figure comes from walking actual book levels.

## Cross-exchange spot arbitrage

You buy the base asset on exchange A (walking A's asks) and at the same moment sell the same amount on exchange B (walking B's bids). This requires inventory positioned in advance: quote currency on A and base currency on B. No funds move per trade.

**Pairing.** Markets are paired when they share the base asset and have quote currencies that are equal or declared equivalent in `quote_equivalents` (for example `[["USD","USDC"]]`, which adds an FX cost). For each pair of venues, the direction with the larger top-of-book discrepancy is evaluated.

**Sizing.**

1. The maximum size is the smallest of three caps:
   - the trade cap `max_trade_gbp`, converted to the quote currency;
   - the crossing depth: the base quantity for which A's ask stays below B's bid;
   - inventory: the quote balance on A, net of fees, and the base balance on B.
2. Candidate sizes are taken at every cumulative level boundary up to that maximum, plus the maximum itself.
3. The candidate with the highest net profit is chosen.

**Costs** (`n` = notional, `b` = basis points):

| Component | Formula |
|---|---|
| Gross profit | sell VWAP proceeds − buy VWAP cost (executable, depth-walked) |
| Fees | buy cost × taker(A) + sell proceeds × taker(B) |
| Slippage buffer | (buy + sell notional) × `slippage_buffer_bps` |
| Latency cost | (buy + sell notional) × `latency_cost_bps` |
| Rebalancing cost | buy notional × `rebalancing_cost_bps` |
| FX cost | sell notional × `fx_cost_bps` (only when quote currencies differ) |
| Expected leg-failure cost | buy notional × `leg_failure_prob` × `leg_failure_loss_bps` |
| **Estimated net profit** | gross − all of the above |
| Net return | net ÷ buy notional |

*Depth slippage* (the cost of walking levels compared with top of book) is reported separately. It is already inside gross profit and is not subtracted twice.

**Worked example** (this is a unit test):

- Inputs: Kraken ask 50,000, Coinbase bid 50,500, 0.1% fees, £25 cap at 0.8 GBP/USD.
- Size: 0.000625 BTC, which is $31.25.
- Gross profit: $0.3125. Fees: $0.0628.
- Estimated net profit: $0.1713, a net return of 0.548%.

## Triangular arbitrage

You convert a start currency through two others and back on a single exchange, for example USD → BTC → ETH → USD.

- **Cycle discovery.** All valid 3-hop cycles are found from the exchange's available markets for each configured start currency (`start_currencies`, default USD, GBP, USDC, USDT, EUR). Each hop is a BUY (spend quote, walk asks) or a SELL (spend base, walk bids). Cycles are cached per set of symbols.
- **Simulation.** Each hop runs in sequence against depth, with lot-size rounding, minimum-order checks and the taker fee deducted from the currency received. Each hop spends what the previous hop actually received. Unspent dust from rounding on the first leg is excluded from the notional.
- **Sizing.** `triangular_size_steps` (default 6) evenly spaced sizes are tried, up to the smaller of the £ cap and the start-currency balance. The size with the best net profit is chosen. If any hop cannot be fully filled, that size is skipped. When no size works, the result is `insufficient_liquidity`.
- **Costs.** Fees come from two simulation passes, one with fees and one without. The slippage buffer and latency cost are charged per leg (×3), plus the expected leg-failure cost. There is no rebalancing cost, because everything stays on one exchange.

## Rejection reasons

| Reason | Meaning |
|---|---|
| `spread_non_positive` | No gross discrepancy at top of book |
| `fees_exceed_profit` | Gross is positive, but costs make net ≤ 0 |
| `below_threshold` | Net is positive but under `min_net_profit_pct` |
| `insufficient_liquidity` | Depth cannot support a profitable size |
| `insufficient_inventory` | The required balance is zero on the needed exchange |
| `below_min_order` | Size is under the exchange's minimum amount or cost |
| `stale_data` | A book is older than `max_data_age_ms` |
| `exchange_unavailable` | An exchange connection is unhealthy |
| `no_valuation` | The trade cannot be converted to GBP |

The scanner shows these with plain-English explanations. An opportunity is **qualified** only when it has no rejection reasons. Execution additionally requires approval from the risk engine.

**Confidence** (0–1) falls as the underlying data ages, reaching 0 at `max_data_age_ms`. It is set to 0 for a crossed book.

**Opportunity age** is how long a route has continuously shown a positive gross spread.

## Parameters (`StrategyParams`)

| Parameter | Default | Notes |
|---|---|---|
| `min_net_profit_pct` | 0.30 | Placeholder, not a recommendation |
| `max_trade_gbp` | 25 | |
| `max_data_age_ms` | 5000 | |
| `slippage_buffer_bps` | 5 | |
| `latency_ms` / `latency_cost_bps` | 250 / 2 | |
| `rebalancing_cost_bps` | 10 | |
| `fx_cost_bps` | 10 | |
| `leg_failure_prob` / `leg_failure_loss_bps` | 0.02 / 50 | |
| `quote_equivalents` | none | |
| `start_currencies` | USD, GBP, USDC, USDT, EUR | |
| `fee_overrides` | none | Keyed by `exchange` or `exchange:SYMBOL` |
| `triangular_size_steps` | 6 | |

Strategy configurations are versioned in the Strategy Manager. Every save creates a new immutable version, and older versions can be restored. Backtests record the config they used.

**Never risk-free.** Both strategies carry execution risk. Legs can fail independently, prices move during latency, and inventory carries market exposure. The expected leg-failure cost is a modelled expectation, not a bound on losses.
