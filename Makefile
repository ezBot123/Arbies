.PHONY: install install-backend install-frontend test test-pg lint migrate dev-backend dev-synthetic dev-frontend build audit docker

install: install-backend install-frontend

install-backend:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

install-frontend:
	cd frontend && npm ci

test:
	cd backend && .venv/bin/pytest -q

# run the integration tests against PostgreSQL (database must exist and be disposable)
test-pg:
	cd backend && ARBIES_TEST_DATABASE_URL=$${ARBIES_TEST_DATABASE_URL:-postgresql+asyncpg://arbies:arbies@localhost:5432/arbies_test} .venv/bin/pytest -q

lint:
	cd backend && .venv/bin/ruff check arbies tests
	cd frontend && npx tsc -b && npm run lint

migrate:
	cd backend && .venv/bin/alembic upgrade head

dev-backend:
	cd backend && .venv/bin/uvicorn arbies.api.app:create_app --factory --reload --port 8000

dev-synthetic:
	cd backend && ARBIES_MARKET_DATA_MODE=synthetic .venv/bin/uvicorn arbies.api.app:create_app --factory --reload --port 8000

dev-frontend:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

audit:
	cd backend && .venv/bin/pip-audit --skip-editable
	cd frontend && npm audit --omit=dev

docker:
	CODE_REVISION=$$(git rev-parse --short=12 HEAD) docker compose up --build -d
