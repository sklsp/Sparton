"""Worker loop (workers.worker): claim/execute/retry/dead-letter and clean shutdown."""

import pytest

from app.core.jobs import transport
from workers import worker as worker_module
from workers.worker import Worker


class FakeBackend:
    """In-memory queue backend that records every interaction with the worker."""

    def __init__(self, entries=(), on_claim=None):
        self.entries = list(entries)
        self.on_claim = on_claim  # optional callback(entry) right after an entry is claimed
        self.claims = []
        self.completed = []
        self.requeued = []
        self.dead_lettered = []
        self.reclaims = []
        self.reclaimed_count = 0

    def claim(self, worker_id, timeout_seconds=1.0):
        self.claims.append({"worker_id": worker_id, "timeout_seconds": timeout_seconds})
        if not self.entries:
            return None
        entry = self.entries.pop(0)
        if self.on_claim is not None:
            self.on_claim(entry)
        return entry

    def complete(self, entry):
        self.completed.append(entry)

    def requeue(self, entry, *, delay_seconds=0):
        self.requeued.append((entry, delay_seconds))

    def dead_letter(self, entry, error):
        self.dead_lettered.append((entry, error))

    def reclaim_stale(self, older_than_seconds):
        self.reclaims.append(older_than_seconds)
        return self.reclaimed_count


@pytest.fixture()
def worker():
    return Worker()


@pytest.fixture()
def run_env(monkeypatch):
    """Neutralise logging config and signal installation for run(); returns installed handlers."""
    installed = {}
    monkeypatch.setattr(worker_module, "configure_logging", lambda level: None)
    monkeypatch.setattr(
        worker_module.signal, "signal", lambda signum, handler: installed.__setitem__(signum, handler)
    )
    return installed


def _entry(**fields):
    entry = {"id": "job-1", "type": "ok", "payload": {"x": 1}}
    entry.update(fields)
    return entry


# --- _work_once -------------------------------------------------------------------


def test_work_once_returns_false_when_nothing_claimed(worker):
    backend = FakeBackend()

    assert worker._work_once(backend) is False

    assert len(backend.claims) == 1
    assert backend.completed == []
    assert (worker.processed, worker.failed) == (0, 0)


def test_work_once_completes_job_and_counts_processed(worker, monkeypatch):
    seen = {}

    def handler(payload):
        seen["payload"] = payload

    monkeypatch.setattr(transport, "_handlers", {"ok": handler})
    entry = _entry()
    backend = FakeBackend(entries=[entry])

    assert worker._work_once(backend) is True

    assert seen == {"payload": {"x": 1}}
    assert entry["worker_id"] == worker.worker_id
    assert backend.completed == [entry]
    assert (worker.processed, worker.failed) == (1, 0)


def test_work_once_requeues_with_base_delay_on_first_failure(worker, monkeypatch):
    def handler(payload):
        raise ValueError("boom")

    monkeypatch.setattr(transport, "_handlers", {"ok": handler})
    entry = _entry()
    backend = FakeBackend(entries=[entry])

    assert worker._work_once(backend) is True

    assert (worker.processed, worker.failed) == (0, 1)
    assert entry["retry_count"] == 1
    assert backend.requeued == [(entry, 5.0)]
    assert backend.dead_lettered == []


def test_work_once_requeues_with_growing_delay_on_second_failure(worker, monkeypatch):
    def handler(payload):
        raise ValueError("boom")

    monkeypatch.setattr(transport, "_handlers", {"ok": handler})
    entry = _entry(retry_count=1)
    backend = FakeBackend(entries=[entry])

    worker._work_once(backend)

    assert entry["retry_count"] == 2
    assert backend.requeued == [(entry, 15.0)]


def test_work_once_dead_letters_after_max_attempts(worker, monkeypatch):
    def handler(payload):
        raise ValueError("boom")

    monkeypatch.setattr(transport, "_handlers", {"ok": handler})
    entry = _entry(retry_count=2)
    backend = FakeBackend(entries=[entry])

    worker._work_once(backend)

    assert worker.failed == 1
    assert backend.requeued == []
    dead_entry, error = backend.dead_lettered[0]
    assert dead_entry is entry
    assert error == "boom"


def test_work_once_unknown_job_type_is_a_failure(worker, monkeypatch):
    monkeypatch.setattr(transport, "_handlers", {})
    entry = _entry(type="nope")
    backend = FakeBackend(entries=[entry])

    worker._work_once(backend)

    assert (worker.processed, worker.failed) == (0, 1)
    assert entry["retry_count"] == 1
    assert backend.requeued == [(entry, 5.0)]


def test_work_once_unknown_type_dead_letters_with_message(worker, monkeypatch):
    monkeypatch.setattr(transport, "_handlers", {})
    entry = _entry(type="nope", retry_count=2)
    backend = FakeBackend(entries=[entry])

    worker._work_once(backend)

    _, error = backend.dead_lettered[0]
    assert "No handler registered for job type 'nope'" in error


# --- run ---------------------------------------------------------------------------


def test_run_returns_zero_when_stop_requested_before_start(worker, monkeypatch, run_env):
    backend = FakeBackend()
    monkeypatch.setattr(worker_module, "get_backend", lambda: backend)
    worker.request_stop()

    assert worker.run() == 0

    # The loop body never ran: no claims, and both signal handlers were installed.
    assert backend.claims == []
    assert set(run_env) == {worker_module.signal.SIGINT, worker_module.signal.SIGTERM}
    # Bound methods are recreated per access, so compare by equality, not identity.
    assert run_env[worker_module.signal.SIGINT] == worker.request_stop
    assert run_env[worker_module.signal.SIGTERM] == worker.request_stop


def test_run_processes_claimed_job_then_stops(worker, monkeypatch, run_env):
    def handler(payload):
        pass

    monkeypatch.setattr(transport, "_handlers", {"ok": handler})
    entry = _entry()
    backend = FakeBackend(entries=[entry], on_claim=lambda e: worker.request_stop())
    monkeypatch.setattr(worker_module, "get_backend", lambda: backend)

    assert worker.run() == 0

    assert backend.completed == [entry]
    assert (worker.processed, worker.failed) == (1, 0)


def test_run_writes_heartbeat_and_reclaims_stale(worker, monkeypatch, run_env):
    beats = []
    backend = FakeBackend()
    backend.client = object()
    backend.reclaimed_count = 2  # exercise the "reclaimed abandoned jobs" warning path

    def fake_beat(client, hostname):
        beats.append((client, hostname))
        worker.request_stop()  # exit the loop after this iteration

    monkeypatch.setattr(worker_module, "write_heartbeat", fake_beat)
    monkeypatch.setattr(worker_module, "HEARTBEAT_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(worker_module, "RECLAIM_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(worker_module, "get_backend", lambda: backend)

    assert worker.run() == 0

    assert beats == [(backend.client, worker.hostname)]
    assert backend.reclaims == [worker_module.STALE_PROCESSING_SECONDS]
    assert len(backend.claims) == 1  # one empty poll before the stop took effect
