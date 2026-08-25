"""Athena run orchestration: create, execute (inline or pooled), resume.

Adapted from Ares `app/agent/runner.py`. Each worker opens a fresh DB
session; runs persisted as WAITING_FOR_APPROVAL can be resumed by any
worker after an approval is resolved.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.orm import Session

from app.agent.engine import AgentEngine
from app.agent.registry import ToolContext, ToolRegistry
from app.core.config import settings
from app.core.database.base import SessionLocal
from app.core.database.domain_models import AgentRun, RunStatus
from app.llm import get_llm_provider

logger = logging.getLogger(__name__)

_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=settings.agent_max_workers, thread_name_prefix="athena"
        )
    return _executor


def build_registry() -> ToolRegistry:
    """Assemble the full multi-domain tool registry."""
    from app.agent.tools import register_all

    registry = ToolRegistry()
    register_all(registry)
    return registry


def build_engine(db: Session) -> AgentEngine:
    llm = get_llm_provider()
    registry = build_registry()
    return AgentEngine(db=db, llm=llm, registry=registry)


def create_run(db: Session, message: str, session_id: str = "default",
               organization_id: int | None = None,
               project_id: int | None = None) -> AgentRun:
    run = AgentRun(
        organization_id=organization_id,
        project_id=project_id,
        session_id=session_id,
        user_request=message,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def execute_run(run_id: int) -> None:
    """Synchronously start a run with its own session."""

    def action(db: Session, run: AgentRun) -> None:
        engine = build_engine(db)
        engine.ctx.organization_id = run.organization_id
        engine.ctx.project_id = run.project_id
        engine.start(run)

    _with_run(run_id, action)


def resume_run(run_id: int) -> None:
    def action(db: Session, run: AgentRun) -> None:
        engine = build_engine(db)
        engine.ctx.organization_id = run.organization_id
        engine.ctx.project_id = run.project_id
        engine.resume(run)

    _with_run(run_id, action)


def submit_run(run_id: int) -> str:
    """Dispatch a run inline (tests/dev) or on the thread pool."""
    if settings.agent_run_inline:
        execute_run(run_id)
        return "inline"
    _get_executor().submit(execute_run, run_id)
    return "queued"


def submit_resume(run_id: int) -> str:
    if settings.agent_run_inline:
        resume_run(run_id)
        return "inline"
    _get_executor().submit(resume_run, run_id)
    return "queued"


def shutdown() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


def _with_run(run_id: int, action) -> None:
    db = SessionLocal()
    try:
        run = db.get(AgentRun, run_id)
        if run is None or run.status in RunStatus.terminal():
            return
        action(db, run)
    except Exception:  # noqa: BLE001
        logger.exception("Agent worker failed for run %s", run_id)
    finally:
        db.close()
