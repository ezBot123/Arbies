#!/usr/bin/env bash
# Arbies local setup (macOS / Linux / WSL2).
#
# Checks prerequisites, creates a git-ignored .env with generated secrets (live trading OFF),
# installs dependencies, prepares PostgreSQL, applies migrations and runs the live-feed check.
# Safe to re-run: existing .env and database are kept.
#
#   ./scripts/setup_local.sh            # use a local PostgreSQL server
#   ./scripts/setup_local.sh --docker   # run PostgreSQL in Docker instead
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
USE_DOCKER=0
[[ "${1:-}" == "--docker" ]] && USE_DOCKER=1

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; MISSING=1; }
info() { printf '\n\033[1m%s\033[0m\n' "$*"; }
MISSING=0

info "1. Prerequisites"
PY=""
for c in python3.13 python3.12 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then PY=$c; break; fi
done
[[ -n "$PY" ]] && ok "Python $($PY -V 2>&1 | cut -d' ' -f2) ($PY)" || bad "Python 3.12+ not found — install from https://www.python.org/downloads/ or your package manager"
if command -v node >/dev/null && node -e 'process.exit(+process.versions.node.split(".")[0] >= 20 ? 0 : 1)'; then
  ok "Node $(node -v)"
else
  bad "Node.js 20+ not found — install from https://nodejs.org/ (LTS)"
fi
command -v npm >/dev/null && ok "npm $(npm -v)" || bad "npm not found (comes with Node.js)"
command -v git >/dev/null && ok "git" || bad "git not found"
if [[ $USE_DOCKER == 1 ]]; then
  if command -v docker >/dev/null && docker info >/dev/null 2>&1; then ok "Docker running"; else bad "Docker not installed or not running — start Docker Desktop"; fi
else
  command -v psql >/dev/null && ok "psql client" || bad "PostgreSQL not found — install PostgreSQL 16 (macOS: brew install postgresql@16 && brew services start postgresql@16) or re-run with --docker"
fi
[[ $MISSING == 1 ]] && { echo; echo "Install the missing items above, then re-run this script."; exit 1; }

info "2. Configuration (.env)"
if [[ -f .env ]]; then
  ok ".env already exists (kept unchanged)"
else
  PW=$($PY -c 'import secrets; print(secrets.token_urlsafe(18))')
  DBPW=$($PY -c 'import secrets; print(secrets.token_urlsafe(18))')
  sed -e "s#^POSTGRES_PASSWORD=.*#POSTGRES_PASSWORD=$DBPW#" \
      -e "s#^ARBIES_DATABASE_URL=.*#ARBIES_DATABASE_URL=postgresql+asyncpg://arbies:$DBPW@localhost:5432/arbies#" \
      -e "s#^ARBIES_ADMIN_PASSWORD=.*#ARBIES_ADMIN_PASSWORD=$PW#" \
      .env.example > .env
  chmod 600 .env
  ok "created .env (admin password and DB password generated; ARBIES_SECRET_KEY set after install)"
fi
grep -q '^ARBIES_LIVE_TRADING_ENABLED=false' .env && ok "live trading disabled" || { echo "  ! ARBIES_LIVE_TRADING_ENABLED is not false in .env — refusing to continue"; exit 1; }
grep -q '^ARBIES_MARKET_DATA_MODE=live' .env && ok "market data mode: live (real public exchange data)" || echo "  ! market data mode is not 'live' in .env"
set -a; . ./.env; set +a

info "3. Dependencies"
if [[ ! -x backend/.venv/bin/python ]]; then "$PY" -m venv backend/.venv; fi
backend/.venv/bin/pip install -q --upgrade pip
backend/.venv/bin/pip install -q -e "backend[dev]"
ok "backend dependencies"
if ! grep -q '^ARBIES_SECRET_KEY=..' .env; then
  KEY=$(backend/.venv/bin/python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')
  $PY - "$KEY" <<'PYEOF'
import re, sys, pathlib
p = pathlib.Path(".env"); s = p.read_text()
p.write_text(re.sub(r"(?m)^ARBIES_SECRET_KEY=.*$", "ARBIES_SECRET_KEY=" + sys.argv[1], s))
PYEOF
  ok "generated ARBIES_SECRET_KEY"
fi
(cd frontend && npm ci --no-audit --no-fund --silent && npm run build --silent >/dev/null)
ok "frontend dependencies and production build"

info "4. Database"
DBPW_CUR=$(sed -n 's#^ARBIES_DATABASE_URL=postgresql+asyncpg://arbies:\([^@]*\)@.*#\1#p' .env)
if [[ $USE_DOCKER == 1 ]]; then
  docker compose up -d db
  until docker compose exec -T db pg_isready -U arbies >/dev/null 2>&1; do sleep 1; done
  ok "PostgreSQL running in Docker (compose service 'db')"
else
  # create role and database if missing (uses your local superuser; may prompt for a password)
  if psql -d postgres -tAc "select 1" >/dev/null 2>&1; then PSQL="psql -d postgres"; else PSQL="sudo -u postgres psql"; fi
  $PSQL -tAc "select 1 from pg_roles where rolname='arbies'" | grep -q 1 || $PSQL -qc "create role arbies login password '$DBPW_CUR'"
  $PSQL -tAc "select 1 from pg_database where datname='arbies'" | grep -q 1 || $PSQL -qc "create database arbies owner arbies"
  ok "database 'arbies' ready"
fi
(cd backend && .venv/bin/alembic upgrade head >/dev/null && .venv/bin/alembic check >/dev/null)
ok "migrations applied and match the models"

info "5. Tests"
(cd backend && .venv/bin/pytest -q | tail -1)

info "6. Live exchange feeds (public data, no API keys)"
(cd backend && .venv/bin/python -m arbies.tools.verify_live) || echo "  ! some feeds failed — see errors above (firewall/VPN/proxy, or exchange outage)"

info "Done"
echo "  Start:     make dev-backend    (API + dashboard on http://localhost:8000)"
echo "  Log in:    user 'admin', password = ARBIES_ADMIN_PASSWORD in .env"
echo "  Recording: after a few minutes run"
echo "             (cd backend && .venv/bin/python -m arbies.tools.verify_live --recording)"
