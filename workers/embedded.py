"""In-process job worker for single-process runs.

Production runs `python -m workers.worker` replicas against Redis. Locally
there is no Redis and no second process, so the API drains the queue itself
on a background thread.

It polls the durable ``Job`` table rather than the transport: ``claim_next``
already claims atomically via ``UPDATE ... WHERE status = QUEUED``, so this
is the same contract the standalone worker uses, minus the transport. The
inline transport backend is deliberately not used here — it executes
handlers synchronously, which would block the POST that queued the job for
as long as the crawl takes.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from app.core.database.base import SessionLocal
from app.core.database.domain_models import Job
from app.core.jobs.queue import claim_next, record_failure
from app.core.jobs.transport import get_handler
from app.core.observability.metrics import inc, observe

import workers.handlers  # noqa: F401 — registers the domain job handlers

logger = logging.getLogger(__name__)

POLL_SECONDS = 1.0


class EmbeddedWorker:
    """Background thread that claims and runs queued jobs."""

    def __init__(self) -> None:
        self.worker_id = f"embedded:{uuid.uuid4().hex[:8]}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="sparton-embedded-worker", daemon=True
        )
        self._thread.start()
        logger.info("Embedded worker %s started", self.worker_id)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info("Embedded worker %s stopped", self.worker_id)

    # -- internals ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                worked = self._work_once()
            except Exception:  # noqa: BLE001 — the poll loop must outlive any job
                logger.exception("Embedded worker poll failed")
                worked = False
            if not worked:
                self._stop.wait(POLL_SECONDS)

    def _work_once(self) -> bool:
        db = SessionLocal()
        try:
            job = claim_next(db, self.worker_id)
            if job is None:
                return False

            handler = get_handler(job.type)
            if handler is None:
                logger.error("No handler registered for job type %r", job.type)
                record_failure(db, job, f"No handler registered for job type {job.type!r}")
                return True

            job_id, job_type = job.id, job.type
            # Handlers open their own session, so hand over plain data and let
            # them own the row while they run.
            payload = {**(job.payload or {}), "job_id": job_id}
            started = time.monotonic()
            try:
                handler(payload)
                inc("worker_jobs_total", outcome="completed")
                observe("worker_job_duration_seconds", time.monotonic() - started)
                logger.info("Job %s (%s) finished in %.1fs", job_id, job_type,
                            time.monotonic() - started)
            except Exception as exc:  # noqa: BLE001 — one bad job must not stop the worker
                inc("worker_jobs_total", outcome="failed")
                logger.exception("Job %s (%s) failed", job_id, job_type)
                db.expire_all()
                current = db.get(Job, job_id)
                if current is not None:
                    record_failure(db, current, str(exc))
            return True
        finally:
            db.close()


__all__ = ["EmbeddedWorker"]
