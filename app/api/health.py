"""System health, readiness, metrics, and tool introspection.

``/live`` and ``/ready`` are deliberately unauthenticated: orchestrators probe
them without credentials. ``/health``, ``/metrics`` and ``/tools`` describe
internal state, so they require a principal.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from sqlalchemy import text

from app.agent.runner import build_registry
from app.core.auth.api import current_user
from app.core.config import settings
from app.core.database.base import engine
from app.core.observability.metrics import render as render_metrics
from app.llm import get_llm_provider

router = APIRouter(tags=["system"])


@router.get("/live")
def live() -> dict:
    """Liveness: process is up. Deliberately dependency-free."""
    return {"status": "live"}


@router.get("/ready")
def ready() -> dict:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:  # noqa: BLE001
        return Response(status_code=503)


@router.get("/health")
def health(user: Annotated[object, Depends(current_user)] = None) -> dict:
    db_ok = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:  # noqa: BLE001
        pass

    llm = get_llm_provider()
    from app.core.features import describe as describe_features

    return {
        "status": "ok" if db_ok else "degraded",
        "database": {"ok": db_ok, "url_scheme": settings.database_url.split(":", 1)[0]},
        "llm": {
            "provider": llm.name,
            "available": llm.is_available(),
            "model": getattr(llm, "default_model", None) or getattr(llm, "model", None),
        },
        "agent_tools": len(build_registry()),
        "features": describe_features(),
    }


@router.get("/metrics")
def metrics(user: Annotated[object, Depends(current_user)] = None) -> Response:
    # Scraped by Prometheus with X-API-Key, or by a signed-in operator.
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")


@router.get("/tools")
def tools(user: Annotated[object, Depends(current_user)] = None) -> dict:
    # Tool names, descriptions and full JSON schemas are internal surface.
    registry = build_registry()
    return {"count": len(registry), "tools": [t.to_info() for t in registry.list()]}


__all__ = ["router"]
