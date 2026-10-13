# Deployment

## Docker Compose (recommended)

```bash
cp .env.example .env
# Required: POSTGRES_PASSWORD, ARBIES_ADMIN_PASSWORD.
# Strongly recommended: ARBIES_SECRET_KEY (needed to store exchange credentials):
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
make docker            # = CODE_REVISION=$(git rev-parse --short=12 HEAD) docker compose up --build -d
```

- `db`: PostgreSQL 16, with data in the `pgdata` volume.
- `app`: a multi-stage image. Node builds the dashboard; Python 3.12-slim runs FastAPI and serves it. It runs as a non-root user and applies `alembic upgrade head` on every start.
- The app is published on **127.0.0.1:8000** only. To reach it remotely, put a TLS reverse proxy in front.
- Healthcheck: `GET /api/health`.

### Verification

The image was built and run in development, with Postgres and the app as separate containers. Migrations applied on start, the healthcheck reported `healthy`, and the API and dashboard were served.

Two caveats from that run:

- The development sandbox needed a local CA injected to reach package registries. That workaround lives outside the repo and is not needed on a normal machine.
- `docker compose up` itself was not run, because Docker Hub rate-limited the sandbox. Its configuration was validated with `docker compose config`.

## VPS (optional)

1. Choose a host in a jurisdiction where you are allowed to use your exchanges, and keep the OS patched.
2. Install Docker and the compose plugin. Clone the repo and create `.env` as above, with `ARBIES_COOKIE_SECURE=true` and `ARBIES_CORS_ORIGINS='["https://your.domain"]'`.
3. Put a reverse proxy with TLS in front, for example Caddy:
   ```
   your.domain {
     reverse_proxy 127.0.0.1:8000
   }
   ```
   WebSockets (`/ws`) pass through Caddy's `reverse_proxy` without extra configuration.
4. Firewall: allow only 22 and 443, and keep 8000 and 5432 closed.
5. If you add exchange API keys, restrict them to the VPS IP and keep withdrawals disabled.

### Outbound proxies

Some networks only allow HTTPS out through an explicit proxy, such as corporate networks or sandboxed cloud containers. Arbies' exchange clients respect the standard environment there: `HTTPS_PROXY` (and `NO_PROXY`) covers REST and the CCXT Pro WebSocket proxy option, and `SSL_CERT_FILE` (or `REQUESTS_CA_BUNDLE`) supplies a TLS-inspecting proxy's CA. TLS verification is never turned off. With none of these set, connections are direct. See `backend/arbies/market_data/ccxt_config.py`.

## Database

- **Backups:**
  ```bash
  docker compose exec db pg_dump -U arbies arbies | gzip > arbies-$(date +%F).sql.gz
  ```
- **Size:** market data dominates storage. At 1 snapshot per second for 18 markets with 20 levels, expect very roughly 2–3 GB per day uncompressed (estimate; measure on your deployment).
- **Controlling growth:** use `ARBIES_DATA_RETENTION_DAYS` (applied hourly) and `ARBIES_RECORD_INTERVAL_MS`.

## Monitoring

| Signal | Where to find it |
|---|---|
| Health | `GET /api/health` (unauthenticated) for uptime checks |
| Component status | `GET /api/system/status` (authenticated): exchange health, scanner timing, recorder rows and errors, paper status, kill switch |
| Logs | Structured; set `ARBIES_LOG_JSON=true` for JSON output to ship to a log stack |
| Feed and recorder health | The System Health agent, run on demand or on a schedule with `ARBIES_AGENTS_SCHEDULE_ENABLED=true`. It works without an LLM. |
| Risk and connection events | Persisted, and shown on Overview and Settings |

## Upgrades

```bash
git pull && make docker      # rebuilds the image; migrations run automatically on start
```

Backtest runs record the code revision they used, so results from different builds stay distinguishable.

## Without Docker

Run PostgreSQL. Then:

```bash
make install
make migrate
cd frontend && npm run build
cd ../backend && .venv/bin/uvicorn arbies.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

The backend serves `frontend/dist` automatically when it exists.
