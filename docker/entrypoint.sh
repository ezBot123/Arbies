#!/bin/sh
set -e
# apply database migrations before starting
alembic upgrade head
exec uvicorn arbies.api.app:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers
