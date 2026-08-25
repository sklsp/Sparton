"""Durable, multi-process job queue backed by the application database.

Why the database instead of Redis: the stack already guarantees durable
rows, migrations and transactions. A claim guarded by an
UPDATE ... WHERE status='QUEUED' gives atomic cross-process claiming on
both SQLite (serialized writes) and PostgreSQL (row locks) without new
infrastructure. Redis is used as the *transport* (see transport.py); the DB
row remains the source of truth for business state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database.domain_models import Job, JobStatus, utcnow
from app.core.jobs import MAX_ATTEMPTS, RETRY_DELAYS_SECONDS, can_transition


def job_signature(payload: dict[str, Any]) -> str:
    """Deterministic idempotency signature for a job request."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def enqueue(
    db: Session,
    *,
    type: str,
    payload: dict[str, Any],
    organization_id: int | None = None,
    project_id: int | None = None,
    created_by: int | None = None,
    priority: int = 5,
    idempotency_key: str | None = None,
) -> tuple[Job, bool]:
    """Create a durable job row. Returns (job, created).

    With an idempotency key, a duplicate submission returns the existing
    QUEUED/RUNNING job instead of creating a second one. Idempotency is
    per-tenant: two organizations submitting the same work are independent.
    """
    scope_prefix = f"org{organization_id}:" if organization_id else ""
    signature = idempotency_key or f"{scope_prefix}{type}:{job_signature(payload)}"
    active = [JobStatus.QUEUED.value, JobStatus.RUNNING.value]

    existing = (
        db.execute(
            select(Job)
            .where(Job.idempotency_key == signature)
            .where(Job.status.in_(active))
            .order_by(Job.id.desc())
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing, False

    job = Job(
        type=type,
        payload=payload,
        organization_id=organization_id,
        project_id=project_id,
        created_by=created_by,
        priority=priority,
        idempotency_key=signature,
        max_attempts=MAX_ATTEMPTS,
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent process enqueued the same signature first: return its
        # row only if it is still active; otherwise allow a fresh submission.
        db.rollback()
        existing = (
            db.execute(
                select(Job)
                .where(Job.idempotency_key == signature)
                .where(Job.status.in_(active))
            )
            .scalars()
            .first()
        )
        if existing is not None:
            return existing, False
        stale = db.execute(select(Job).where(Job.idempotency_key == signature)).scalars().first()
        if stale is not None:
            stale.idempotency_key = None
            db.commit()
        db.add(job)
        db.commit()
        db.refresh(job)
        return job, True
    db.refresh(job)
    return job, True


def claim_next(db: Session, worker_id: str, job_type: str | None = None) -> Job | None:
    """Atomically claim the highest-priority queued job for this worker.

    The UPDATE ... WHERE status=QUEUED guard means a concurrent worker's
    claim fails harmlessly and it simply picks the next row.
    """
    now = utcnow()
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.QUEUED.value)
        .order_by(Job.priority.asc(), Job.created_at.asc())
        .limit(10)
    )
    if job_type:
        stmt = stmt.where(Job.type == job_type)
    candidates = db.execute(stmt).scalars().all()
    for job in candidates:
        if job.run_after is not None and as_utc(job.run_after) > now:
            continue
        updated = db.execute(
            update_job_status_stmt(job.id, JobStatus.RUNNING, JobStatus.QUEUED, worker_id=worker_id)
        )
        db.commit()
        if updated.rowcount:
            db.refresh(job)
            return job
    return None


def update_job_status_stmt(
    job_id: int,
    target: JobStatus,
    expected: JobStatus,
    worker_id: str | None = None,
):
    values: dict[str, Any] = {"status": target.value}
    if target is JobStatus.RUNNING:
        values["started_at"] = utcnow()
    elif target in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        values["completed_at"] = utcnow()
    if worker_id:
        values["worker_id"] = worker_id
    return (
        update(Job)
        .where(Job.id == job_id)
        .where(Job.status == expected.value)
        .values(**values)
    )


def transition(db: Session, job: Job, target: JobStatus) -> Job:
    """Apply a validated state transition to a loaded job."""
    current = JobStatus(job.status)
    if not can_transition(current, target):
        raise InvalidTransition(f"{current.value} -> {target.value} is not allowed")
    job.status = target.value
    if target is JobStatus.RUNNING and job.started_at is None:
        job.started_at = utcnow()
    if target in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
        job.completed_at = utcnow()
    db.commit()
    db.refresh(job)
    return job


def record_failure(db: Session, job: Job, error: str) -> Job:
    """Mark a failure, requeueing with backoff when attempts remain."""
    job.retry_count += 1
    job.error = error[:2000]
    attempts_used = job.retry_count
    if attempts_used < min(job.max_attempts or MAX_ATTEMPTS, MAX_ATTEMPTS):
        # Requeue; the worker's poll delay provides the backoff window.
        job.status = JobStatus.QUEUED.value
        job.stage = f"retry {attempts_used} scheduled"
        delay = RETRY_DELAYS_SECONDS[min(attempts_used - 1, len(RETRY_DELAYS_SECONDS) - 1)]
        job.run_after = utcnow() + timedelta(seconds=delay)
    else:
        job.status = JobStatus.FAILED.value
        job.stage = "failed"
        job.completed_at = utcnow()
    db.commit()
    db.refresh(job)
    return job


def request_cancel(db: Session, job: Job) -> Job:
    """Cooperative cancellation: long-running handlers poll this flag."""
    if job.status in {JobStatus.QUEUED.value, JobStatus.RUNNING.value}:
        if job.status == JobStatus.QUEUED.value:
            transition(db, job, JobStatus.CANCELLED)
        else:
            job.cancel_requested = True
            db.commit()
            db.refresh(job)
    return job


def queue_depth(db: Session) -> dict[str, int]:
    rows = db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all()
    return {status: int(count) for status, count in rows}


def due_jobs(db: Session, limit: int = 10) -> list[Job]:
    now: datetime = utcnow()
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.QUEUED.value)
        .order_by(Job.priority.asc(), Job.created_at.asc())
        .limit(limit)
    )
    jobs = list(db.execute(stmt).scalars().all())
    return [j for j in jobs if j.run_after is None or as_utc(j.run_after) <= now]


def as_utc(value: datetime) -> datetime:
    from app.core.database.models import as_utc as model_as_utc

    return model_as_utc(value)
