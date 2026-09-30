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

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.api import (
    admin as admin_api,
    agent_api,
    auth,
    billing,
    commerce,
    ecommerce,
    health,
    intelligence,
)

# `create` (generation/datasets/training) and `knowledge` (documents) are
# imported inside the feature-flag branches below, not here. Both pull in
# sentence-transformers and faiss, which is roughly nine gigabytes of PyTorch,
# and importing them unconditionally meant the *product* could not start
# without them -- including in a container that will never serve a single
# document route. A disabled domain having no routes is not much of a feature
# if it still has to be installed.
from app.core.config import settings
from app.core.database.base import Base, engine
from app.core.features import Domain, is_enabled
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

    # Routers are registered conditionally. A disabled domain has NO routes at
    # all rather than routes that 403 — strictly less attack surface
    # (docs/DECISIONS.md D-020).
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(admin_api.router)
    # Billing is part of the product: Checkout, the Portal and the webhook.
    app.include_router(billing.router)

    # The product.
    if is_enabled(Domain.COMMERCE):
        app.include_router(commerce.router)
    if is_enabled(Domain.AGENT):
        app.include_router(agent_api.router)
    if is_enabled(Domain.INTELLIGENCE):
        app.include_router(ecommerce.router)
    if is_enabled(Domain.RESEARCH):
        app.include_router(intelligence.router)
    if is_enabled(Domain.DOCUMENTS):
        from app.api import knowledge  # lazy: see the import note above

        app.include_router(knowledge.router)
    if is_enabled(Domain.GENERATION) or is_enabled(Domain.DATASETS) or is_enabled(
        Domain.TRAINING
    ):
        from app.api import create  # lazy: see the import note above

        app.include_router(create.router)

    # Dashboard. Mounted last so every API route above wins on a path clash.
    if WEB_DIR.is_dir():
        app.mount("/app", DashboardFiles(directory=WEB_DIR, html=True), name="app")

    @app.get("/", include_in_schema=False)
    async def root() -> FileResponse:
        """The public landing page. Everything a visitor needs before signup."""
        landing = WEB_DIR / "landing.html"
        if landing.is_file():
            return FileResponse(landing, media_type="text/html")
        return RedirectResponse(url="/app/")

    # The landing page is served from `/`, but its assets live in the same
    # directory as the dashboard, which is mounted at `/app`. Without these the
    # page renders unstyled and its script 404s. Serving them explicitly keeps
    # the dashboard's own `/app/...` paths untouched.
    #
    # `ui.js` belongs here too: `landing.js` imports it for `h`/`fill`, and a
    # module whose import 404s fails to parse, so the *whole* script is dead and
    # the pricing table silently never appears. Found by driving a browser --
    # the page looked fine, because the static copy renders without JavaScript.
    for _asset, _media in (
        ("landing.css", "text/css"),
        ("landing.js", "text/javascript"),
        ("styles.css", "text/css"),
        ("ui.js", "text/javascript"),
    ):
        def _serve(_asset: str = _asset, _media: str = _media):
            path = WEB_DIR / _asset
            if not path.is_file():
                raise HTTPException(status_code=404, detail="Not found")
            return FileResponse(path, media_type=_media)

        app.add_api_route(
            f"/{_asset}", _serve, methods=["GET"], include_in_schema=False
        )

    # Legal pages. The landing page links to these, so they are real routes
    # rather than dead anchors: a pricing page that 404s on "Terms" is a
    # signal to a prospective customer about how the rest is run.
    for _page in ("privacy", "terms", "dpa"):
        def _legal(_page: str = _page):
            path = WEB_DIR / "legal" / f"{_page}.html"
            if not path.is_file():
                raise HTTPException(status_code=404, detail="Not found")
            return FileResponse(path, media_type="text/html")

        app.add_api_route(
            f"/legal/{_page}", _legal, methods=["GET"], include_in_schema=False
        )

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    return app


app = create_app()
