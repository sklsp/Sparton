"""SPARTON application factory.

One FastAPI app: platform core (auth, tenancy, jobs, observability) plus
every domain router. Apollo's CORS wildcard is replaced with an explicit
origin allowlist; Ares' middleware/metrics stack is applied globally.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import admin as admin_api
from app.api import agent_api, auth, create, ecommerce, health, intelligence, knowledge
from app.core.config import settings
from app.core.database.base import Base, engine
from app.core.observability.logging_config import configure_logging
from app.core.observability.middleware import instrument_requests

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    # Dev convenience: create tables when they don't exist yet. Production
    # uses Alembic migrations (see migrations/).
    if settings.is_sqlite:
        Base.metadata.create_all(bind=engine)
    logger.info("SPARTON API starting (provider=%s)", settings.llm_provider)
    yield
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
    instrument_requests(app)

    # Routers — one coherent surface, no per-project prefixes.
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(knowledge.router)
    app.include_router(agent_api.router)
    app.include_router(intelligence.router)
    app.include_router(ecommerce.router)
    app.include_router(create.router)
    app.include_router(admin_api.router)

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app


app = create_app()
