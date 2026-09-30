"""Worker liveness heartbeat.

The worker serves no HTTP, so nothing can ask it "are you alive?" the way the
API is asked. Instead it refreshes a key in Redis on every poll, and
`python -m workers.healthcheck` reads that key and exits non-zero when it is
stale.

Two decisions worth stating, because both are the kind of thing that looks
simpler than it is:

**Keyed by hostname, not by the worker's id.** The healthcheck runs as a
separate process inside the same container and has no way to learn the worker's
uuid. The hostname it shares with the worker is the only stable handle. Several
workers in one host each write their own key, and any one of them being fresh
satisfies the check -- which is the right answer, since one live worker means
the queue is being drained.

**A TTL, not a timestamp comparison.** Redis evicts the key, so "no key" means
"stale" with no arithmetic to get wrong and nothing left behind by a dead
worker. The age is still readable when you want to see *how* stale.
"""

from __future__ import annotations

import socket
import time
from typing import Any

#: Namespace for heartbeat keys, kept in one place because the writer
#: (workers/worker.py) and the reader (workers/healthcheck.py) must not drift.
HEARTBEAT_PREFIX = "sparton:worker:heartbeat:"

#: How long a beat stays valid. Must be comfortably longer than the
#: orchestrator's healthcheck interval, or a perfectly healthy worker looks dead
#: in the gap between two checks.
DEFAULT_TTL_SECONDS = 90.0

#: Age above which a beat is refused even if the key somehow still exists.
DEFAULT_MAX_AGE_SECONDS = 90.0


def heartbeat_key(hostname: str | None = None) -> str:
    """The Redis key for one host's worker."""
    return HEARTBEAT_PREFIX + (hostname or socket.gethostname())


def write_heartbeat(
    client: Any,
    hostname: str | None = None,
    *,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> bool:
    """Record that the worker loop is alive. Returns False if Redis is down.

    A failure here is deliberately not fatal to the worker: it keeps draining
    the queue, and the healthcheck will report the outage through the fact that
    no beat is landing.
    """
    if client is None:
        return False
    try:
        client.set(heartbeat_key(hostname), str(now if now is not None else time.time()), ex=int(ttl_seconds))
        return True
    except Exception:  # noqa: BLE001 - telemetry must never stop the worker
        return False


def heartbeat_age(
    client: Any,
    hostname: str | None = None,
    *,
    now: float | None = None,
) -> float | None:
    """Seconds since the last beat, or None if there is no usable beat.

    None means "we do not know that the worker is alive" and is deliberately
    distinct from a large number: a missing key and an old key are different
    failures and the healthcheck reports them differently.
    """
    if client is None:
        return None
    try:
        raw = client.get(heartbeat_key(hostname))
    except Exception:  # noqa: BLE001
        return None
    if raw is None:
        return None
    try:
        beat = float(raw if isinstance(raw, (int, float)) else raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return None
    reference = now if now is not None else time.time()
    age = reference - beat
    # A beat from the future means clock skew between containers, not a worker
    # from next Tuesday. Treat it as fresh rather than failing the deploy.
    return max(0.0, age)


def heartbeat_is_fresh(
    client: Any,
    hostname: str | None = None,
    *,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    now: float | None = None,
) -> bool:
    age = heartbeat_age(client, hostname, now=now)
    return age is not None and age <= max_age_seconds


def redis_client():
    """A Redis client for the heartbeat, or None when Redis is not configured."""
    from app.core.config import settings

    if not settings.redis_url:
        return None
    try:
        import redis

        return redis.Redis.from_url(settings.redis_url, decode_responses=True)
    except Exception:  # noqa: BLE001 - including "redis is not installed"
        return None


__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "HEARTBEAT_PREFIX",
    "heartbeat_age",
    "heartbeat_is_fresh",
    "heartbeat_key",
    "redis_client",
    "write_heartbeat",
]
