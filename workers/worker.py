"""Standalone queue worker (Ares foundation).

Claims jobs from the transport, executes registered handlers, and handles
the failure ladder: retry with exponential backoff, then dead-letter.
Multiple workers can run concurrently; claim() is atomic so a job is
delivered to exactly one worker at a time (at-least-once across crashes).

Local development does not need this process: with no REDIS_URL the API
uses the inline backend. Production runs `python -m app.worker` replicas
with REDIS_URL set.
"""

from __future__ import annotations

import os
import signal
import socket
import sys
import time
import uuid

from app.core.config import settings
from app.core.jobs.heartbeat import write_heartbeat
from app.core.jobs.transport import get_backend, get_handler
from app.core.observability.logging_config import configure_logging, get_logger
from app.core.observability.metrics import inc, observe

import workers.handlers  # noqa: F401 — register domain job handlers

logger = get_logger(__name__)

POLL_TIMEOUT_SECONDS = 1.0
RECLAIM_INTERVAL_SECONDS = 30.0
STALE_PROCESSING_SECONDS = 300.0
# Exponential backoff for retries (seconds): 5, 15, 45 — capped.
BACKOFF_BASE_SECONDS = 5.0
BACKOFF_CAP_SECONDS = 600.0
MAX_ATTEMPTS = 3
#: How often to refresh the liveness beat. Well under the key's TTL, so a
#: single missed beat is never mistaken for a dead worker.
HEARTBEAT_INTERVAL_SECONDS = 15.0


class Worker:
    def __init__(self) -> None:
        self.hostname = socket.gethostname()
        self.worker_id = f"{self.hostname}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self._stop = False
        self.processed = 0
        self.failed = 0

    def request_stop(self, *_: object) -> None:
        logger.info("Shutdown requested; finishing current job")
        self._stop = True

    def run(self) -> int:
        configure_logging(settings.log_level)
        logger.info("Worker %s starting (redis=%s)", self.worker_id, bool(settings.redis_url))
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        backend = get_backend()
        last_reclaim = 0.0
        last_beat = 0.0
        # The liveness beat is what the container healthcheck reads, since the
        # worker serves no HTTP. It is refreshed here rather than inside
        # _work_once so that a worker with an empty queue still looks alive --
        # which is the common case, and it is exactly when a wedged worker and
        # an idle one are hardest to tell apart.
        beat_client = getattr(backend, "client", None)
        while not self._stop:
            now = time.monotonic()
            if beat_client is not None and now - last_beat >= HEARTBEAT_INTERVAL_SECONDS:
                write_heartbeat(beat_client, self.hostname)
                last_beat = now
            if now - last_reclaim >= RECLAIM_INTERVAL_SECONDS:
                reclaimed = backend.reclaim_stale(STALE_PROCESSING_SECONDS)
                if reclaimed:
                    logger.warning("Reclaimed %d abandoned job(s)", reclaimed)
                last_reclaim = now
            if not self._work_once(backend):
                time.sleep(0.2)
        logger.info(
            "Worker %s stopped: processed=%d failed=%d",
            self.worker_id,
            self.processed,
            self.failed,
        )
        return 0

    def _work_once(self, backend) -> bool:
        entry = backend.claim(self.worker_id, timeout_seconds=POLL_TIMEOUT_SECONDS)
        if entry is None:
            return False
        entry["worker_id"] = self.worker_id
        started = time.monotonic()
        handler = get_handler(entry.get("type", ""))
        try:
            if handler is None:
                raise RuntimeError(f"No handler registered for job type {entry.get('type')!r}")
            handler(entry.get("payload", {}))
            backend.complete(entry)
            self.processed += 1
            inc("worker_jobs_total", outcome="completed")
            observe("worker_job_duration_seconds", time.monotonic() - started)
            logger.info("Job %s completed in %.1fs", entry.get("id"), time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001 - one bad job must not kill the worker
            self.failed += 1
            inc("worker_jobs_total", outcome="failed")
            logger.exception("Job %s failed", entry.get("id"))
            retry_count = int(entry.get("retry_count", 0)) + 1
            if retry_count >= MAX_ATTEMPTS:
                logger.error("Job %s moved to dead letter after %d attempts", entry.get("id"), retry_count)
                backend.dead_letter(entry, str(exc))
            else:
                delay = min(BACKOFF_BASE_SECONDS * (3 ** (retry_count - 1)), BACKOFF_CAP_SECONDS)
                logger.warning("Requeueing job %s (attempt %d) in %.0fs", entry.get("id"), retry_count, delay)
                entry["retry_count"] = retry_count
                backend.requeue(entry, delay_seconds=delay)
        return True


def main() -> int:
    return Worker().run()


if __name__ == "__main__":
    sys.exit(main())
