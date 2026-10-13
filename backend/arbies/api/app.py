"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from arbies.config import Settings, get_settings
from arbies.core.models import utcnow
from arbies.logging_setup import configure_logging, get_logger
from arbies.services.runtime import Runtime

from .deps import ws_user
from .routes import api, public

log = get_logger(__name__)


def stream_payload(rt: Runtime) -> dict:
    now = utcnow()
    opps = [o.to_dict(now) for o in rt.scanner.current if o.gross_spread_pct > 0][:50]
    tops = []
    for (ex, sym), ob in sorted(rt.hub.books.items()):
        tops.append({"exchange": ex, "symbol": sym, "bid": str(ob.best_bid) if ob.best_bid else None,
                     "ask": str(ob.best_ask) if ob.best_ask else None, "age_ms": round(ob.age_ms(now)),
                     "source": ob.source.value})
    paper = None
    if rt.paper.core is not None:
        snap = rt.paper.snapshot()
        paper = {k: snap.get(k) for k in ("status", "value_gbp", "total_pnl_gbp", "daily_realised_pnl_gbp",
                                          "session_id", "counters")}
    return {
        "type": "tick", "ts": now.isoformat(), "data_source": rt.hub.data_source(),
        "exchanges": {ex: {"status": h.status.value, "healthy": rt.hub.is_healthy(ex, now), "transport": h.transport,
                           "updates": h.updates, "errors": h.errors}
                      for ex, h in rt.hub.health().items()},
        "tops": tops, "opportunities": opps, "scanner": rt.scanner.stats, "paper": paper,
        "kill_switch": rt.global_kill, "live_armed": rt.gate.is_armed(),
    }


def create_app(settings: Settings | None = None, runtime: Runtime | None = None,
               start_market_data: bool = True) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    rt = runtime or Runtime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await rt.start(start_market_data=start_market_data)
        task = asyncio.create_task(_broadcaster(rt))
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await rt.stop()

    app = FastAPI(title="Arbies API", version="0.1.0", lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.rt = rt
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type", "Authorization"])

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error", path=request.url.path)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    app.include_router(public)
    app.include_router(api)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        user = await ws_user(ws)
        if user is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        rt.ws_clients.add(ws)
        try:
            await ws.send_text(json.dumps(stream_payload(rt), default=str))
            while True:
                await ws.receive_text()  # keepalive / ignore client messages
        except WebSocketDisconnect:
            pass
        finally:
            rt.ws_clients.discard(ws)

    # serve the built frontend if present (production single-container mode)
    dist = Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            f = dist / path
            if path and f.is_file() and dist in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app


async def _broadcaster(rt: Runtime) -> None:
    while True:
        await asyncio.sleep(1.0)
        if not rt.ws_clients:
            continue
        try:
            msg = json.dumps(stream_payload(rt), default=str)
        except Exception:  # noqa: BLE001
            log.exception("stream payload failed")
            continue
        for ws in list(rt.ws_clients):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001
                rt.ws_clients.discard(ws)


def main() -> None:  # pragma: no cover - entrypoint
    import uvicorn
    s = get_settings()
    uvicorn.run(create_app(s), host="0.0.0.0", port=8000, log_level=s.log_level.lower())


if __name__ == "__main__":  # pragma: no cover
    main()
