"""Distributed rate limiting (Ares foundation).

Uses a fixed-window counter in Redis so limits are shared across API
replicas. Falls back to the process-local limiter when REDIS_URL is not
configured or Redis is unreachable — local development then works with no
infrastructure, and a Redis outage degrades to per-replica limiting rather
than blocking all traffic.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import HTTPException, Request, status

from app.core.config import settings

_local_lock = threading.Lock()
_local_hits: dict[str, deque[float]] = defaultdict(deque)


def _redis_client():
    if not settings.redis_url:
        return None
    try:
        import redis  # optional dependency, imported lazily

        client = redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=1, socket_timeout=1
        )
        client.ping()
        return client
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 - unreachable Redis must not break requests
        return None


def _hit_local(key: str, limit: int, window_seconds: float) -> None:
    now = time.monotonic()
    with _local_lock:
        bucket = _local_hits[key]
        while bucket and bucket[0] <= now - window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded; slow down and retry shortly",
            )
        bucket.append(now)


def _hit_distributed(client, key: str, limit: int, window_seconds: float) -> None:
    """Atomic INCR + EXPIRE window shared by every replica."""
    window = int(time.time() // window_seconds)
    redis_key = f"ratelimit:{key}:{window}"
    try:
        current = client.incr(redis_key)
        if current == 1:
            client.expire(redis_key, int(window_seconds) + 1)
    except Exception:  # noqa: BLE001 - Redis failure falls back, never blocks
        _hit_local(key, limit, window_seconds)
        return
    if current > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded; slow down and retry shortly",
        )


def check_rate_limit(key: str, limit: int, window_seconds: float = 60.0) -> None:
    """Apply one rate-limit window. Raises HTTP 429 when exceeded."""
    client = _redis_client()
    if client is not None:
        _hit_distributed(client, key, limit, window_seconds)
    else:
        _hit_local(key, limit, window_seconds)


def client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    return (
        (forwarded.split(",")[0].strip() if forwarded else None)
        or (request.client.host if request.client else "unknown")
    )


def rate_limit(
    *,
    limit: int,
    window_seconds: float = 60.0,
    key_by: Callable[[Request], str] | None = None,
) -> Callable:
    """Dependency factory: raises 429 when the caller exceeds `limit`/window."""

    def dependency(request: Request) -> None:
        if settings.rate_limit_disabled:
            return
        identity = key_by(request) if key_by else client_key(request)
        route = request.scope.get("route")
        scope_name = getattr(route, "path", request.url.path)
        check_rate_limit(f"{identity}:{scope_name}", limit, window_seconds)

    return dependency


def reset_limits() -> None:
    with _local_lock:
        _local_hits.clear()
