# Risk management

`backend/arbies/risk/engine.py` holds a deterministic `RiskEngine`, which `TradingCore` consults before every simulated execution, in both backtests and paper trading. The live order manager adds further gates on top; see [SECURITY.md](SECURITY.md). The engine contains no AI. Agents can read its status but have no tool that changes it.

## Checks applied to every opportunity

| Check | Default | Violation |
|---|---|---|
| Global kill switch | off | `kill_switch` |
| Circuit breaker (N consecutive failed executions → cooldown) | 3 failures → 300 s | `circuit_breaker` |
| Opportunity is qualified by the strategy filters | — | `opportunity_not_qualified` |
| Max position (notional) | £25 | `max_position_size` |
| Min estimated net return | 0.30% | `min_net_profit` |
| Max depth slippage as % of notional | 0.50% | `max_slippage` |
| Max market-data age | 5000 ms | `stale_data` |
| Min data confidence | 0.2 | `low_confidence` |
| Max daily realised loss | £25 | `max_daily_loss` |
| Max outstanding (open) order exposure | £50 | `max_open_order_exposure` |
| Exchange connections healthy | required | `exchange_unhealthy` |
| Duplicate protection (same route within window) | 2000 ms | `duplicate_order` |
| Trade rate limit | 30/min | `trade_rate_limit` |
| Sufficient balances after the trade (projected) | — | `insufficient_balance` |
| Max total crypto exposure* | £500 | `max_total_exposure` |
| Max per-exchange exposure* | £300 | `max_exchange_exposure` |
| Max per-asset exposure* | £300 | `max_asset_exposure` |
| Inventory imbalance (one exchange's share of an asset)* | 90% | `inventory_imbalance` |
| Opportunity can be valued in GBP | — | `no_valuation` |

\* Exposure is the GBP value of non-cash inventory: everything except GBP, USD, EUR, USDC and USDT, a list configurable through `cash_assets`. Exposure and imbalance checks block only trades that would leave a limit breached **and** make it worse. Trades that reduce risk are always allowed.

## Controls

- **Kill switch.** It is activated from the dashboard header (one click, with confirmation) or with `POST /api/risk/kill-switch`. It applies to the live risk engine and the current paper session, and it disarms live trading. **Releasing it requires the admin password.** Paper sessions created while it is active start with it already on.
- **Circuit breaker.** It trips automatically after consecutive failed or partial executions and raises a critical risk event.
- **Daily loss limit.** Once realised P&L for the UTC day reaches the limit, new trades are blocked for the rest of the day.
- **Duplicate-order protection.**
  - Route level: the same opportunity key is blocked within a time window.
  - Order level: a client-order-id registry rejects reuse. Live orders send the id to the exchange as an idempotency key where the exchange supports one.
- **Limit changes.** Paper-session limits can be changed from Settings and are audited. Live limits need password re-authentication. `update_limits` raises `PermissionError` for live mode unless the caller passes explicit user approval.
- **Risk events** (circuit breaker, daily loss, kill switch, limit changes) are persisted to `risk_events` and shown on the Overview and Settings pages.

## Differences between modes

| Mode | Behaviour |
|---|---|
| Backtest | Fresh engine per run with the run's configured limits. "Exchange healthy" means the exchange produced data within `max_data_age_ms`; data gaps count as outages. |
| Paper | One engine per session, with limits from the session config. Exchange health comes from the live connection monitor. |
| Live | A separate engine (`Runtime.live_risk`) with its own limits. Order submission additionally needs the live-trading gate and order pre-validation against exchange rules. |

## Emergency procedures

- `LiveOrderManager.emergency_cancel_all()` cancels every open order on every connected exchange, and it is allowed even when trading is disarmed.
- `reconcile_balances(expected, actual)` lists differences between internal records and balances reported by the exchanges.
- Live exchange clients are wrapped in `SafeExchange`. It permanently refuses withdrawal and transfer methods, and it refuses trading methods unless the credentials have trade permission.

## Tests

`tests/test_portfolio_risk_sim.py` and `tests/test_security_agents_live.py` cover:

- position, profit, staleness, health, duplicate, balance, daily-loss, circuit-breaker and exposure checks;
- live-limit approval;
- every condition of the live gate;
- duplicate client order ids, and refusal while the kill switch is active.
