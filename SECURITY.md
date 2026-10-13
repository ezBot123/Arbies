# Security

## Principles

- Exchange credentials live only on the backend, encrypted at rest. They are never sent to the frontend or to AI agents.
- **Paper trading is the default.** Real orders need several independent gates (below).
- **There is no withdrawal or transfer code path.**
- AI agents can read, simulate and propose. They cannot execute.

## Authentication

- **Admin account.** There is a single admin account. The password comes from `ARBIES_ADMIN_PASSWORD`; if that is unset, a random password is generated on first start, printed once in the log, and stored as a scrypt hash.
- **Login.** It is rate-limited to 10 attempts per 5 minutes per IP. Successful and failed attempts are written to the audit log.
- **Sessions.** Each session is a random 256-bit token. The database stores only its SHA-256 hash, with an expiry (`ARBIES_SESSION_TTL_HOURS`, default 12). The browser holds it as an `httpOnly`, `SameSite=Strict` cookie; set `ARBIES_COOKIE_SECURE=true` behind HTTPS. API clients can send it as a Bearer token instead.
- **Protected endpoints.** Every `/api/*` endpoint except `/api/health` and `/api/auth/login` requires a session. So does the WebSocket.
- **Rate limits.** These sensitive endpoints are rate-limited: login, kill switch, credential storage, live-risk changes, live arming, live balance reads and agent runs.
- **Password re-authentication.** It is required to store credentials, change live risk limits, release the kill switch and arm live trading.
- **Headers.** Security headers are set (`X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`), and CORS is restricted to configured origins.

## Secrets

- **Configuration.** Secrets come from environment variables or a `.env` file. `.env` is git-ignored, and `.env.example` contains placeholders only.
- **Credential encryption.** Exchange API credentials are encrypted with Fernet (AES-128-CBC + HMAC-SHA256), using `ARBIES_SECRET_KEY`. Without the key, credentials cannot be stored.
- **What the API returns.** It only ever returns exchange name, permission level, sandbox flag and a 12-character key fingerprint.
- **`/api/settings`.** It returns `public_dict()`, which leaves out every secret field. A test checks this.
- **Key hygiene.** Use **read-only** exchange API keys with **withdrawals disabled**, and restrict them to your server's IP where the exchange supports it.

## Live-trading gates

All of the following must hold before any real order is sent:

1. `ARBIES_LIVE_TRADING_ENABLED=true` in the backend environment. The default is `false`.
2. **The arming flow:** request a preview listing exchanges, credential permissions, risk limits and risks, then within 10 minutes submit the preview id, your password and the exact confirmation phrase. Arming expires after 1 hour, lives in memory only (a restart always disarms), and is refused while the kill switch is active.
3. Credentials stored with `permissions=trade` for each exchange. They default to `read_only`.
4. The risk engine: kill switch, circuit breaker and limits.
5. Order pre-validation against exchange rules (lot size, tick size, minimums, per-order notional cap).
6. A unique client order id (duplicate protection).
7. `SafeExchange` method whitelisting.

A UI toggle cannot bypass these. The checks run in the backend, and an API call that skips the UI hits the same gates.

**Automated live execution of strategies is not activated in this release.** No code path connects `TradingCore` to the live order manager.

## AI agent permissions

- **The gateway.** Agents act only through `ToolGateway`, which enforces a per-agent allowlist, validates arguments with strict Pydantic models (`extra="forbid"`) and logs every call — denied ones too — to `agent_tasks.tool_calls`.
- **What the registry allows.** It grants only the `read`, `simulate` and `propose` permissions. It refuses to register any tool whose name suggests live trading, orders, withdrawals, transfers, credentials, secrets, the kill switch, risk-limit changes or arming.
- **Who can simulate.** Only the Strategy Optimisation agent has simulation tools (backtest and walk-forward optimisation).
- **Out-of-sample data.** Walk-forward test results and test-segment backtests are sealed from agent tools, and agents cannot run the out-of-sample test.
- **Recommendations.** Agent recommendations are stored as proposals. Approving one records the human decision and applies nothing automatically.
- **Inside the LLM tool loop.** The LLM sees only the allowed tools, and a hallucinated or forbidden tool call comes back as an error tool result. This is tested with a scripted fake model.
- **Cost and failures.** LLM spend is estimated per task and capped by `ARBIES_AI_MONTHLY_BUDGET_USD`. Refusals and tool errors are recorded, not hidden.

## Audit logging

These actions are written to `audit_logs` with actor, target, detail and IP:

- login and logout;
- backtest submission and deletion;
- paper start, pause, stop and reset;
- strategy changes;
- agent runs and recommendation decisions;
- kill switch;
- all settings changes;
- credential storage and removal;
- live preview, arm, refusal and disarm;
- live balance reads.

They are visible under Settings → Security & audit.

## Dependency scanning

`make audit` runs `pip-audit` and `npm audit`. At the time of writing, both report **0 known vulnerabilities**: react-router was upgraded to 7.18.4 to clear advisories GHSA-wrjc-x8rr-h8h6 and GHSA-337j-9hxr-rhxg. CI runs both audits.

## Reporting

This is a personal research project. Report security issues privately to the repository owner.
