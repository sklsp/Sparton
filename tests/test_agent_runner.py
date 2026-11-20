"""Athena runner (app.agent.runner): create, inline and pooled dispatch, resume.

The LLM is always the deterministic provider from conftest. Pooled tests flip
``settings.agent_run_inline`` off and let a real worker thread run against the
same SQLite file; the engine is built with ``check_same_thread=False`` for
exactly this (app/core/database/base.py).
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import select

import app.agent.runner as runner
from app.core.config import settings
from app.core.database.base import SessionLocal
from app.core.database.domain_models import AgentRun, AgentStep, RunStatus
from app.core.database.identity import Organization, Project
from app.llm import LLMError


def _wait_for_terminal(run_id: int, timeout: float = 10.0) -> AgentRun:
    """Poll fresh sessions until the pooled worker reaches a terminal status."""
    deadline = time.monotonic() + timeout
    last_status = None
    while True:
        db = SessionLocal()
        try:
            run = db.get(AgentRun, run_id)
            assert run is not None
            last_status = run.status
            if run.status in RunStatus.terminal():
                return run
        finally:
            db.close()
        if time.monotonic() > deadline:
            pytest.fail(f"run {run_id} still {last_status} after {timeout}s")
        time.sleep(0.05)


def _step_count(db, run_id: int) -> int:
    return len(db.execute(select(AgentStep).where(AgentStep.agent_run_id == run_id)).scalars().all())


# --- create_run ---------------------------------------------------------------


def test_create_run_persists_a_running_row_with_defaults(db_session):
    run = runner.create_run(db_session, "check competitor prices")

    assert run.id is not None
    assert run.status == RunStatus.RUNNING
    assert run.user_request == "check competitor prices"
    assert run.session_id == "default"
    assert run.organization_id is None
    assert run.project_id is None


def test_create_run_keeps_the_tenant_scope(db_session):
    # Real org and project rows: this engine enforces foreign keys, so a
    # dangling project_id would be rejected.
    org = Organization(name="Tenant Scope Org")
    db_session.add(org)
    db_session.flush()
    project = Project(organization_id=org.id, name="P1")
    db_session.add(project)
    db_session.flush()

    run = runner.create_run(
        db_session, "hello", session_id="s-1", organization_id=org.id, project_id=project.id
    )

    assert run.session_id == "s-1"
    assert run.organization_id == org.id
    assert run.project_id == project.id


# --- execute_run: the branches _with_run handles -------------------------------


def test_execute_run_completes_a_new_run(db_session, llm):
    run = runner.create_run(db_session, "What can you do?")

    runner.execute_run(run.id)

    db_session.refresh(run)
    assert run.status == RunStatus.COMPLETED
    assert run.final_response == "Deterministic response."
    assert run.error is None
    assert _step_count(db_session, run.id) >= 2


def test_execute_run_missing_job_is_a_noop(db_session, llm):
    runner.execute_run(999_999)

    assert db_session.get(AgentRun, 999_999) is None


def test_execute_run_skips_a_terminal_run(db_session, llm):
    run = runner.create_run(db_session, "already finished")
    run.status = RunStatus.COMPLETED
    db_session.commit()

    runner.execute_run(run.id)

    db_session.refresh(run)
    assert run.status == RunStatus.COMPLETED
    assert run.final_response is None  # the engine never ran on it
    assert _step_count(db_session, run.id) == 0


def test_execute_run_swallows_engine_errors(db_session, llm, monkeypatch):
    """A crash inside the action is logged, not raised: one bad job must not
    take down the caller."""

    def boom(db):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(runner, "build_engine", boom)
    run = runner.create_run(db_session, "will fail to build")

    runner.execute_run(run.id)  # must not raise

    db_session.refresh(run)
    assert run.status == RunStatus.RUNNING  # the engine never started it
    assert _step_count(db_session, run.id) == 0


def test_execute_run_persists_failed_status_on_llm_error(db_session, llm, monkeypatch):
    def broken_chat(messages, tools=None, **kwargs):
        raise LLMError("provider down")

    monkeypatch.setattr(llm, "chat", broken_chat)
    run = runner.create_run(db_session, "will fail on the model")

    runner.execute_run(run.id)

    db_session.refresh(run)
    assert run.status == RunStatus.FAILED
    assert "provider down" in (run.error or "")
    assert run.final_response == "The run could not be completed: provider down"


# --- resume_run -----------------------------------------------------------------


# --- submit: inline and pooled ---------------------------------------------------


def test_submit_run_inline_returns_inline_and_finishes(db_session, llm, monkeypatch):
    monkeypatch.setattr(settings, "agent_run_inline", True)
    run = runner.create_run(db_session, "hello")

    assert runner.submit_run(run.id) == "inline"

    db_session.refresh(run)
    assert run.status == RunStatus.COMPLETED


def test_submit_resume_inline_returns_inline_and_finishes(db_session, llm, monkeypatch):
    monkeypatch.setattr(settings, "agent_run_inline", True)
    run = runner.create_run(db_session, "hello")
    run.status = RunStatus.WAITING_FOR_APPROVAL
    db_session.commit()

    assert runner.submit_resume(run.id) == "inline"

    db_session.refresh(run)
    assert run.status == RunStatus.COMPLETED


def test_submit_run_queued_runs_on_the_pool(db_session, llm, monkeypatch):
    monkeypatch.setattr(settings, "agent_run_inline", False)
    runner._executor = None
    try:
        run = runner.create_run(db_session, "queued hello")

        assert runner.submit_run(run.id) == "queued"

        finished = _wait_for_terminal(run.id)
        assert finished.status == RunStatus.COMPLETED
        assert finished.final_response == "Deterministic response."
    finally:
        runner.shutdown()


def test_submit_resume_queued_runs_on_the_pool(db_session, llm, monkeypatch):
    monkeypatch.setattr(settings, "agent_run_inline", False)
    runner._executor = None
    try:
        run = runner.create_run(db_session, "queued resume")
        run.status = RunStatus.WAITING_FOR_APPROVAL
        db_session.commit()

        assert runner.submit_resume(run.id) == "queued"

        finished = _wait_for_terminal(run.id)
        assert finished.status == RunStatus.COMPLETED
    finally:
        runner.shutdown()


# --- the executor pool itself ------------------------------------------------------


def test_get_executor_caches_one_pool():
    runner._executor = None
    try:
        first = runner._get_executor()
        second = runner._get_executor()

        assert first is second
    finally:
        runner.shutdown()


def test_shutdown_releases_and_is_idempotent():
    runner._executor = None
    pool = runner._get_executor()
    assert pool is not None

    runner.shutdown()
    assert runner._executor is None

    runner.shutdown()  # no executor left: must be a silent no-op
    assert runner._executor is None


def test_shutdown_cancels_unstarted_work(db_session, llm, monkeypatch):
    """cancel_futures=True drops queued work that no worker has started yet."""
    monkeypatch.setattr(settings, "agent_run_inline", False)
    monkeypatch.setattr(settings, "agent_max_workers", 1)
    runner._executor = None

    started = threading.Event()
    gate = threading.Event()

    def blocked(run_id: int) -> None:
        started.set()
        gate.wait(timeout=5)

    try:
        monkeypatch.setattr(runner, "execute_run", blocked)
        first = runner.create_run(db_session, "holds the worker")
        second = runner.create_run(db_session, "waits in queue")

        assert runner.submit_run(first.id) == "queued"
        assert started.wait(5), "the worker never picked up the first run"
        # The single worker is busy, so this future cannot have started.
        assert runner.submit_run(second.id) == "queued"

        runner.shutdown()

        db_session.refresh(first)
        db_session.refresh(second)
        assert first.status == RunStatus.RUNNING  # blocked never touched it
        assert second.status == RunStatus.RUNNING  # cancelled before it ran
    finally:
        gate.set()
        runner._executor = None

