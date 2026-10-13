# ---- frontend build ----
FROM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- backend runtime (serves API + built dashboard) ----
FROM python:3.12-slim AS app
# recorded with every backtest for reproducibility: docker build --build-arg CODE_REVISION=$(git rev-parse --short=12 HEAD)
ARG CODE_REVISION=unknown
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 ARBIES_CODE_REVISION=${CODE_REVISION}
WORKDIR /app/backend
RUN useradd --create-home --uid 1000 arbies
COPY backend/pyproject.toml ./
COPY backend/arbies ./arbies
RUN pip install --upgrade pip && pip install .
COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY --from=frontend /app/frontend/dist /app/frontend/dist
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && chown -R arbies /app
USER arbies
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health').status==200 else 1)"
ENTRYPOINT ["/entrypoint.sh"]
