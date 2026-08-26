"""SPARTON application factory.

One FastAPI app: platform core (auth, tenancy, jobs, observability), every
domain router, and the dashboard. Apollo's CORS wildcard is replaced with an
explicit origin allowlist; Ares' middleware/metrics stack is applied globally.

The dashboard is a static ES-module SPA under app/web, served by this same
process — no second toolchain, no build step, no separate origin.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import admin as admin_api
from app.api import agent_api, auth, create, ecommerce, health, intelligence, knowledge
from app.core.config import settings
from app.core.database.base import Base, engine
from app.core.observability.logging_config import configure_logging
from app.core.observability.middleware import RequestInstrumentation

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"


class DashboardFiles(StaticFiles):
    """StaticFiles that always revalidates.

    Without an explicit Cache-Control, browsers apply heuristic caching to the
    dashboard's ES modules and keep serving a stale build after a deploy.
    `no-cache` still allows a cheap 304 via the ETag — it only forbids using a
    cached copy without asking.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    # Dev convenience: create tables when they don't exist yet. Production
    # uses Alembic migrations (see migrations/).
    if settings.is_sqlite:
        Base.metadata.create_all(bind=engine)
    logger.info("SPARTON API starting (provider=%s)", settings.llm_provider)

    # Drain the durable job queue in-process. Without this nothing consumes
    # research or generation jobs on a single-process run and they sit at
    # QUEUED forever. Production sets EMBEDDED_WORKER=false and runs
    # `python -m workers.worker` replicas instead.
    worker = None
    if settings.embedded_worker:
        from workers.embedded import EmbeddedWorker

        worker = EmbeddedWorker()
        worker.start()

    yield

    if worker is not None:
        worker.stop()
    from app.agent import runner

    runner.shutdown()
    logger.info("SPARTON API stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="SPARTON",
        description="One unified AI platform: Knowledge, Intelligence, Create.",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Security: explicit origin allowlist (never wildcard + credentials).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Correlation IDs + request metrics.
    app.add_middleware(RequestInstrumentation)

    # Routers — one coherent surface, no per-project prefixes.
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(knowledge.router)
    app.include_router(agent_api.router)
    app.include_router(intelligence.router)
    app.include_router(ecommerce.router)
    app.include_router(create.router)
    app.include_router(admin_api.router)

    # Dashboard. Mounted last so every API route above wins on a path clash.
    if WEB_DIR.is_dir():
        app.mount("/dashboard", DashboardFiles(directory=WEB_DIR, html=True), name="dashboard")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """Browsers get the dashboard; without it, the interactive API docs."""
        return RedirectResponse(url="/dashboard/" if WEB_DIR.is_dir() else "/docs")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app


app = create_app()
