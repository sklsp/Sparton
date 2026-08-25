"""Queue transport abstraction (Ares foundation).

Business logic depends on `QueueBackend`, never on Redis primitives. Two
implementations:

- `RedisQueueBackend`: production transport. Durable list + sorted-set
  structures; jobs survive API/worker restarts; delivery is at-least-once
  via a processing queue with reclaim of abandoned entries.
- `InlineQueueBackend`: executes handlers synchronously in-process. Used by
  tests and by `EMBEDDED_WORKER=true` local development where no Redis is
  running. Not a production transport.
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Callable

from app.core.observability.logging_config import get_logger

logger = get_logger(__name__)

QUEUE_KEY = "sparton:queue"
PROCESSING_KEY = "sparton:processing"
DEAD_LETTER_KEY = "sparton:dead"
DELAYED_KEY = "sparton:delayed"

# Handlers are registered by job type so workers stay decoupled from callers.
Handler = Callable[[dict[str, Any]], None]
_handlers: dict[str, Handler] = {}


def register_handler(job_type: str, handler: Handler) -> None:
    _handlers[job_type] = handler


def get_handler(job_type: str) -> Handler | None:
    return _handlers.get(job_type)


class QueueBackend(ABC):
    @abstractmethod
    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        job_id: str | None = None,
        delay_seconds: float = 0,
    ) -> str:
        """Queue one job; returns its transport id."""

    @abstractmethod
    def claim(self, worker_id: str, timeout_seconds: float = 1.0) -> dict[str, Any] | None:
        """Atomically move one due job from queue to processing."""

    @abstractmethod
    def complete(self, entry: dict[str, Any]) -> None:
        """Remove a successfully processed job from processing."""

    @abstractmethod
    def requeue(self, entry: dict[str, Any], *, delay_seconds: float = 0) -> None:
        """Return a failed job to the queue (retry with backoff)."""

    @abstractmethod
    def dead_letter(self, entry: dict[str, Any], error: str) -> None:
        """Park a permanently failed job for inspection."""

    @abstractmethod
    def reclaim_stale(self, older_than_seconds: float) -> int:
        """Return abandoned processing entries to the queue."""

    @abstractmethod
    def depth(self) -> dict[str, int]:
        """Queue inspection counters."""

    @abstractmethod
    def ping(self) -> bool:
        """Transport reachability."""


class RedisQueueBackend(QueueBackend):
    """Redis transport using atomic list operations.

    claim() uses RPOPLPUSH-style semantics: an entry moves from the queue to
    a per-worker processing list atomically, so two workers can never receive
    the same job, and a crashed worker's entry remains recoverable.
    """

    def __init__(self, redis_client) -> None:
        self.client = redis_client

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        job_id: str | None = None,
        delay_seconds: float = 0,
    ) -> str:
        entry_id = job_id or uuid.uuid4().hex
        entry = json.dumps(
            {
                "id": entry_id,
                "type": job_type,
                "payload": payload,
                "enqueued_at": time.time(),
            }
        )
        if delay_seconds > 0:
            self.client.zadd(DELAYED_KEY, {entry: time.time() + delay_seconds})
        else:
            self.client.lpush(QUEUE_KEY, entry)
        return entry_id

    def claim(self, worker_id: str, timeout_seconds: float = 1.0) -> dict[str, Any] | None:
        self._promote_delayed()
        processing_key = f"{PROCESSING_KEY}:{worker_id}"
        if timeout_seconds <= 0:
            # Non-blocking poll (tests, busy loops): RPOPLPUSH without block.
            result = self.client.rpoplpush(QUEUE_KEY, processing_key)
        else:
            # Atomic blocking move; a job goes to exactly one worker.
            result = self.client.brpoplpush(
                QUEUE_KEY, processing_key, timeout=int(timeout_seconds)
            )
        if result is None:
            return None
        try:
            return json.loads(result)
        except (ValueError, TypeError):
            logger.warning("Discarding malformed queue entry")
            self.client.lrem(processing_key, 1, result)
            return None

    def _promote_delayed(self) -> None:
        due = self.client.zrangebyscore(DELAYED_KEY, "-inf", time.time())
        if not due:
            return
        for entry in due:
            removed = self.client.zrem(DELAYED_KEY, entry)
            if removed:
                self.client.lpush(QUEUE_KEY, entry)

    def complete(self, entry: dict[str, Any]) -> None:
        self.client.lrem(
            f"{PROCESSING_KEY}:{entry.get('worker_id', '')}", 1, json.dumps(entry)
        )

    def requeue(self, entry: dict[str, Any], *, delay_seconds: float = 0) -> None:
        self._remove_from_processing(entry)
        self.enqueue(
            entry.get("type", "job"),
            entry.get("payload", {}),
            job_id=entry.get("id"),
            delay_seconds=delay_seconds,
        )

    def dead_letter(self, entry: dict[str, Any], error: str) -> None:
        self._remove_from_processing(entry)
        failed = dict(entry)
        failed["error"] = error[:2000]
        failed["failed_at"] = time.time()
        self.client.lpush(DEAD_LETTER_KEY, json.dumps(failed))

    def _remove_from_processing(self, entry: dict[str, Any]) -> None:
        self.client.lrem(f"{PROCESSING_KEY}:{entry.get('worker_id', '')}", 1, json.dumps(entry))

    def reclaim_stale(self, older_than_seconds: float) -> int:
        """Return entries parked by workers that died mid-job."""
        reclaimed = 0
        for key in self.client.keys(f"{PROCESSING_KEY}:*"):
            worker_key = key.decode() if isinstance(key, bytes) else key
            entries = self.client.lrange(worker_key, 0, -1)
            for raw in entries:
                try:
                    entry = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if time.time() - entry.get("enqueued_at", 0) > older_than_seconds:
                    if self.client.lrem(worker_key, 1, raw):
                        self.client.lpush(QUEUE_KEY, raw)
                        reclaimed += 1
        return reclaimed

    def depth(self) -> dict[str, int]:
        return {
            "queued": int(self.client.llen(QUEUE_KEY)),
            "delayed": int(self.client.zcard(DELAYED_KEY)),
            "dead_letter": int(self.client.llen(DEAD_LETTER_KEY)),
            "processing": sum(
                int(self.client.llen(k)) for k in self.client.keys(f"{PROCESSING_KEY}:*")
            ),
        }

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except Exception:  # noqa: BLE001
            return False


class InlineQueueBackend(QueueBackend):
    """Synchronous in-process execution for tests and local dev.

    Not a production transport: no durability, no distribution.
    """

    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        job_id: str | None = None,
        delay_seconds: float = 0,
    ) -> str:
        entry = {
            "id": job_id or uuid.uuid4().hex,
            "type": job_type,
            "payload": payload,
            "worker_id": "inline",
        }
        handler = get_handler(job_type)
        if handler is not None:
            handler(payload)
        self.executed.append(entry)
        return entry["id"]

    def claim(self, worker_id: str, timeout_seconds: float = 1.0) -> None:
        return None

    def complete(self, entry: dict[str, Any]) -> None:
        pass

    def requeue(self, entry: dict[str, Any], *, delay_seconds: float = 0) -> None:
        pass

    def dead_letter(self, entry: dict[str, Any], error: str) -> None:
        pass

    def reclaim_stale(self, older_than_seconds: float) -> int:
        return 0

    def depth(self) -> dict[str, int]:
        return {"queued": 0, "delayed": 0, "dead_letter": 0, "processing": 0}

    def ping(self) -> bool:
        return True


_backend: QueueBackend | None = None


def get_backend() -> QueueBackend:
    """Process-wide backend: Redis when configured, inline otherwise."""
    global _backend
    if _backend is None:
        from app.core.config import settings

        if settings.redis_url:
            import redis

            _backend = RedisQueueBackend(
                redis.Redis.from_url(settings.redis_url, decode_responses=True)
            )
        else:
            _backend = InlineQueueBackend()
    return _backend


def set_backend(backend: QueueBackend | None) -> None:
    """Override the process backend (tests inject fakeredis here)."""
    global _backend
    _backend = backend


__all__ = [
    "DEAD_LETTER_KEY",
    "DELAYED_KEY",
    "InlineQueueBackend",
    "PROCESSING_KEY",
    "QUEUE_KEY",
    "QueueBackend",
    "RedisQueueBackend",
    "get_backend",
    "get_handler",
    "register_handler",
    "set_backend",
]
