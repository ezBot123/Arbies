// Contextual explanations for metrics shown across the dashboard.
export const GLOSSARY: Record<string, string> = {
  gross_spread:
    'Top-of-book price discrepancy: (best bid on sell venue − best ask on buy venue) ÷ best ask. Informational only — it ignores depth, fees and costs.',
  gross_profit:
    'Profit from walking the actual order-book depth at the chosen size (volume-weighted prices), before fees and any modelled costs.',
  fees: 'Taker fees for every leg at the configured/exchange fee rates.',
  slippage:
    'Depth slippage is the cost of consuming several book levels (already inside gross profit). The slippage buffer is an extra assumed adverse move between detection and fill.',
  latency_cost: 'Expected adverse price move during the decision-to-fill latency window, per leg.',
  rebalancing_cost: 'Amortised cost of periodically moving inventory between exchanges (withdrawal fees, transfer time).',
  leg_failure_cost: 'Expected cost of one leg failing or partially filling: probability × loss on the unhedged notional.',
  net_profit:
    'Estimated net profit = gross profit − fees − slippage buffer − latency cost − rebalancing cost − FX cost − expected leg-failure cost. An estimate, not a realised result.',
  net_return: 'Estimated net profit ÷ trade notional.',
  max_size: 'Largest size the order books (and your available balances) support before costs.',
  age: 'How long this route has continuously shown a positive gross spread.',
  confidence: 'Data quality score from 0 to 1, decreasing as the underlying order books get older. 0 for stale or crossed books.',
  qualified: 'Passes all strategy filters (net return above threshold, sufficient liquidity and inventory, fresh data). Risk-engine checks happen separately at execution.',
  max_drawdown: 'Largest peak-to-trough fall in portfolio value during the period.',
  fill_ratio: 'Filled quantity ÷ requested quantity across all entry orders.',
  sharpe:
    'Annualised Sharpe ratio from daily equity returns, risk-free rate 0, 365-day year. Only computed with at least 3 daily returns; short backtests do not support it.',
  realised_trade_pnl: 'Sum of per-trade portfolio value changes, valued at prices frozen at decision time (isolates execution quality).',
  market_pnl: 'Change in value of held inventory caused by market price moves (unrealised exposure), not arbitrage.',
  failed_hedge_losses: 'Losses from unwinding mismatched legs after partial or failed fills.',
  simulated: 'Simulated results use the same strategy, risk and fill logic as live trading would, but no real orders are placed.',
  dataset_id: 'Content hash of the exact data used, so the result can be reproduced from its inputs.',
}
