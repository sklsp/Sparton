"""`python -m workers.healthcheck` -- the worker's Docker HEALTHCHECK.

The image carries a HEALTHCHECK that curls the API's `/live`. The worker serves
no HTTP at all, so it inherited a check that can only ever fail, and Docker
marks it unhealthy forever. This module is the worker's own check, wired up in
docker-compose.yml.

Three questions, each answering something the others cannot:

1. **Can we reach the database?** A worker that cannot query will fail every
   job it claims, and would otherwise look perfectly healthy while doing nothing.
2. **Can we reach Redis?** The queue lives there. Without it there is no work to
   claim, and `claim()` would block or throw on every poll.
3. **Is the beat recent?** The only signal that the *loop* is turning. The first
   two stay green while the process is wedged in a job, hung on a socket, or
   spinning in a retry -- which is the failure a worker actually has.

Exits 0 when all three pass, 1 otherwise, printing one line per check so a
`docker inspect` failure says which one broke.
"""

from __future__ import annotations

import sys
from typing import Any

from app.core.jobs.heartbeat import (
    DEFAULT_MAX_AGE_SECONDS,
    heartbeat_age,
    redis_client,
)

#: A beat older than this means the loop is not turning. Must be well above the
#: healthcheck interval (30s in compose) or a healthy worker trips on timing
#: alone.
MAX_AGE_SECONDS = DEFAULT_MAX_AGE_SECONDS


def check_database() -> tuple[bool, str]:
    """One round trip. The worker's handlers all write, so read-only is not
    enough of a probe to trust."""
    try:
        from sqlalchemy import text

        from app.core.database.base import engine

        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, "database ok"
    except Exception as exc:  # noqa: BLE001 - any failure is a failure
        return False, f"database unreachable: {exc}"


def check_redis(client: Any | None = None) -> tuple[bool, str]:
    if client is None:
        client = redis_client()
    if client is None:
        return False, "redis not configured (REDIS_URL is unset)"
    try:
        return (True, "redis ok") if client.ping() else (False, "redis did not answer PING")
    except Exception as exc:  # noqa: BLE001
        return False, f"redis unreachable: {exc}"


def check_heartbeat(
    client: Any | None = None,
    *,
    hostname: str | None = None,
    now: float | None = None,
    max_age_seconds: float = MAX_AGE_SECONDS,
) -> tuple[bool, str]:
    """Is the worker loop still turning?"""
    if client is None:
        client = redis_client()
    if client is None:
        return False, "cannot check a heartbeat without redis"
    age = heartbeat_age(client, hostname, now=now)
    if age is None:
        return False, "no worker heartbeat: the loop is not running, or never started"
    if age > max_age_seconds:
        return (
            False,
            f"worker heartbeat is {age:.0f}s old (limit {max_age_seconds:.0f}s): "
            "the loop is wedged",
        )
    return True, f"worker heartbeat {age:.0f}s old"


def main(argv: list[str] | None = None) -> int:
    """0 when healthy, 1 otherwise. Never raises: a crashing healthcheck reads
    as a passing one in some orchestrators, which is the opposite of useful."""
    checks = (check_database, check_redis, check_heartbeat)
    failed = False
    for check in checks:
        try:
            ok, message = check()
        except Exception as exc:  # noqa: BLE001
            ok, message = False, f"{check.__name__} raised: {exc}"
        if not ok:
            failed = True
        print(f"{'ok  ' if ok else 'FAIL'} {message}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
