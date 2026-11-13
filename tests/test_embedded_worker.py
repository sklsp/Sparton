"""Embedded worker (workers.embedded): DB-backed polling loop and thread lifecycle."""

import time

import pytest

from app.core.database.domain_models import JobStatus
from app.core.jobs import transport
from app.core.jobs.queue import enqueue
from workers.embedded import EmbeddedWorker


@pytest.fixture()
def embedded():
    return EmbeddedWorker()


# --- _work_once -------------------------------------------------------------------


def test_work_once_returns_false_when_nothing_queued(db_session, embedded):
    assert embedded._work_once() is False


def test_work_once_runs_handler_with_job_id_in_payload(db_session, embedded, monkeypatch):
    seen = {}

    def handler(payload):
        seen["payload"] = payload

    monkeypatch.setattr(transport, "_handlers", {"ok": handler})
    job, created = enqueue(db_session, type="ok", payload={"x": 1})
    assert created

    assert embedded._work_once() is True

    assert seen == {"payload": {"x": 1, "job_id": job.id}}
    # Handlers own their row: the worker leaves a successful job RUNNING.
    db_session.refresh(job)
    assert job.status == JobStatus.RUNNING.value


def test_work_once_records_failure_when_handler_raises(db_session, embedded, monkeypatch):
    def handler(payload):
        raise ValueError("boom")

    monkeypatch.setattr(transport, "_handlers", {"ok": handler})
    job, _ = enqueue(db_session, type="ok", payload={})

    assert embedded._work_once() is True

    db_session.refresh(job)
    assert job.retry_count == 1
    assert "boom" in (job.error or "")
    # Attempts remain: the job is requeued with backoff instead of failing.
    assert job.status == JobStatus.QUEUED.value
    assert job.run_after is not None


def test_work_once_records_failure_for_unknown_job_type(db_session, embedded, monkeypatch):
    monkeypatch.setattr(transport, "_handlers", {})
    job, _ = enqueue(db_session, type="nope", payload={})

    assert embedded._work_once() is True

    db_session.refresh(job)
    assert job.retry_count == 1
    assert "No handler registered for job type 'nope'" in (job.error or "")
    assert job.status == JobStatus.QUEUED.value


# --- start / stop -------------------------------------------------------------------


def test_start_spawns_thread_and_stop_joins_it(db_session, embedded):
    embedded.start()
    thread = embedded._thread
    assert thread is not None
    assert thread.is_alive()

    # Starting again must not spawn a second thread.
    embedded.start()
    assert embedded._thread is thread

    embedded.stop(timeout=5.0)

    assert embedded._thread is None
    assert not thread.is_alive()


def test_stop_without_start_is_a_noop(db_session, embedded):
    embedded.stop(timeout=1.0)  # must not raise
    assert embedded._thread is None


def test_loop_survives_work_errors(db_session, embedded, monkeypatch):
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("poll exploded")

    monkeypatch.setattr(embedded, "_work_once", boom)
    embedded.start()
    deadline = time.monotonic() + 3.0
    while calls["n"] < 1 and time.monotonic() < deadline:
        time.sleep(0.05)
    thread = embedded._thread
    embedded.stop(timeout=5.0)

    assert calls["n"] >= 1  # the loop hit the error path at least once
    assert not thread.is_alive()  # ...and still shut down cleanly
