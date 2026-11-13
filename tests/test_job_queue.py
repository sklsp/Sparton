"""Durable job queue (app.core.jobs.queue): enqueue dedup, atomic claim, retries and cancellation."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.core.database.base import SessionLocal
from app.core.database.domain_models import Job, JobStatus, utcnow
from app.core.jobs import MAX_ATTEMPTS, RETRY_DELAYS_SECONDS
from app.core.jobs.queue import (
    as_utc,
    claim_next,
    due_jobs,
    enqueue,
    job_signature,
    queue_depth,
    record_failure,
    request_cancel,
    transition,
    update_job_status_stmt,
)


def _job(db_session, *, type="crawl_shop", payload=None, priority=5, created_at=None, **kwargs):
    """Insert one committed Job row with explicit control over ordering fields."""
    if "max_attempts" not in kwargs:
        kwargs["max_attempts"] = MAX_ATTEMPTS
    job = Job(
        type=type,
        payload=payload or {},
        priority=priority,
        **kwargs,
    )
    if created_at is not None:
        job.created_at = created_at
    db_session.add(job)
    db_session.commit()
    return job


# --- job_signature ----------------------------------------------------------


def test_job_signature_is_stable_across_key_order():
    first = job_signature({"shop_id": 1, "depth": 2})
    second = job_signature({"depth": 2, "shop_id": 1})
    assert first == second
    assert len(first) == 32
    assert all(c in "0123456789abcdef" for c in first)


def test_job_signature_differs_for_different_payloads():
    assert job_signature({"shop_id": 1}) != job_signature({"shop_id": 2})


def test_job_signature_serialises_non_json_native_values():
    stamp = datetime(2026, 11, 12, 8, 30)
    assert job_signature({"since": stamp}) == job_signature({"since": stamp})


# --- enqueue ----------------------------------------------------------------


def test_enqueue_creates_a_queued_job_with_the_given_fields(db_session):
    job, created = enqueue(
        db_session,
        type="crawl_shop",
        payload={"shop_id": 7},
        organization_id=1,
        priority=1,
    )
    assert created is True
    assert job.status == JobStatus.QUEUED
    assert job.stage == "queued"
    assert job.payload == {"shop_id": 7}
    assert job.organization_id == 1
    assert job.priority == 1


def test_enqueue_defaults_priority_to_five_and_max_attempts(db_session):
    job, created = enqueue(db_session, type="crawl_shop", payload={})
    assert created is True
    assert job.priority == 5
    assert job.max_attempts == MAX_ATTEMPTS
    assert job.retry_count == 0


def test_enqueue_returns_existing_job_for_duplicate_signature(db_session):
    first, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})
    second, created = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})
    assert created is False
    assert second.id == first.id
    rows = db_session.query(Job).filter_by(type="crawl_shop").all()
    assert len(rows) == 1


def test_enqueue_returns_existing_running_job_for_duplicate_signature(db_session):
    first, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})
    transition(db_session, first, JobStatus.RUNNING)

    second, created = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})

    assert created is False
    assert second.id == first.id


def test_enqueue_idempotency_is_per_tenant(db_session):
    first, created_a = enqueue(
        db_session, type="crawl_shop", payload={"shop_id": 1}, organization_id=1
    )
    second, created_b = enqueue(
        db_session, type="crawl_shop", payload={"shop_id": 1}, organization_id=2
    )
    assert created_a is True and created_b is True
    assert first.id != second.id


def test_enqueue_global_and_tenant_jobs_do_not_collide(db_session):
    global_job, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})

    tenant_job, created = enqueue(
        db_session, type="crawl_shop", payload={"shop_id": 1}, organization_id=9
    )

    assert created is True
    assert tenant_job.id != global_job.id


# --- explicit keys and conflicts --------------------------------------------


def test_enqueue_explicit_key_dedupes_across_different_payloads(db_session):
    first, _ = enqueue(db_session, type="report", payload={"n": 1}, idempotency_key="rep-1")

    second, created = enqueue(db_session, type="report", payload={"n": 2}, idempotency_key="rep-1")

    assert created is False
    assert second.id == first.id


def test_enqueue_allows_new_job_after_previous_one_completed(db_session):
    first, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})
    transition(db_session, first, JobStatus.RUNNING)
    transition(db_session, first, JobStatus.COMPLETED)

    second, created = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})

    assert created is True
    assert second.id != first.id


def test_enqueue_clears_stale_terminal_key_on_conflict(db_session):
    stale = _job(db_session, type="report", idempotency_key="rep-2")
    transition(db_session, stale, JobStatus.RUNNING)
    transition(db_session, stale, JobStatus.COMPLETED)

    fresh, created = enqueue(db_session, type="report", payload={"n": 1}, idempotency_key="rep-2")

    assert created is True
    assert fresh.id != stale.id
    assert fresh.status == JobStatus.QUEUED
    assert fresh.idempotency_key == "rep-2"
    db_session.refresh(stale)
    assert stale.idempotency_key is None


def test_enqueue_returns_concurrent_winnners_row_when_commit_conflicts(db_session, monkeypatch):
    key = "rep-3"
    winner_id: list[int] = []

    def insert_winner():
        other = SessionLocal()
        try:
            comp = Job(type="report", payload={"n": 1}, idempotency_key=key, max_attempts=MAX_ATTEMPTS)
            other.add(comp)
            other.commit()
            winner_id.append(comp.id)
        finally:
            other.close()

    real_commit = db_session.commit
    state = {"raised": False}

    def flaky_commit():
        if not state["raised"]:
            state["raised"] = True
            # Release our read lock so the simulated "other process" can write.
            db_session.rollback()
            insert_winner()
            raise IntegrityError(
                "INSERT INTO jobs VALUES (...)", {}, Exception("UNIQUE constraint failed: jobs.idempotency_key")
            )
        return real_commit()

    monkeypatch.setattr(db_session, "commit", flaky_commit)
    job, created = enqueue(db_session, type="report", payload={"n": 1}, idempotency_key=key)

    assert created is False
    assert job.id == winner_id[0]


# --- claim_next -------------------------------------------------------------


def test_claim_next_returns_none_when_nothing_is_queued(db_session):
    assert claim_next(db_session, "worker-a") is None


def test_claim_next_claims_highest_priority_first(db_session):
    _job(db_session, priority=5, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    high = _job(db_session, priority=1, created_at=datetime(2026, 1, 2, tzinfo=UTC))

    claimed = claim_next(db_session, "worker-a")

    assert claimed is not None and claimed.id == high.id
    assert claimed.status == JobStatus.RUNNING
    assert claimed.worker_id == "worker-a"
    assert claimed.started_at is not None


def test_claim_next_oldest_first_within_same_priority(db_session):
    old = _job(db_session, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    _job(db_session, created_at=datetime(2026, 1, 2, tzinfo=UTC))

    claimed = claim_next(db_session, "worker-a")

    assert claimed is not None and claimed.id == old.id


def test_claim_next_skips_jobs_that_are_not_due(db_session):
    job = _job(db_session, run_after=utcnow() + timedelta(hours=1))
    assert claim_next(db_session, "worker-a") is None

    job.run_after = utcnow() - timedelta(seconds=5)
    db_session.commit()
    claimed = claim_next(db_session, "worker-a")
    assert claimed is not None and claimed.id == job.id


def test_claim_next_filters_by_job_type(db_session):
    other = _job(db_session, type="report", priority=1, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    wanted = _job(db_session, type="crawl_shop", priority=5, created_at=datetime(2026, 1, 2, tzinfo=UTC))

    claimed = claim_next(db_session, "worker-a", job_type="crawl_shop")

    assert claimed is not None and claimed.id == wanted.id
    db_session.refresh(other)
    assert other.status == JobStatus.QUEUED


def test_claim_next_is_atomic_across_sessions(db_session):
    first = _job(db_session, priority=1, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    second = _job(db_session, priority=5, created_at=datetime(2026, 1, 2, tzinfo=UTC))

    other = SessionLocal()
    try:
        claimed_a = claim_next(other, "worker-a")
        claimed_b = claim_next(db_session, "worker-b")
        claimed_c = claim_next(db_session, "worker-c")
    finally:
        other.close()

    assert claimed_a is not None and claimed_a.id == first.id
    assert claimed_b is not None and claimed_b.id == second.id
    assert claimed_c is None


def test_claim_next_returns_none_when_all_candidates_in_window_are_not_due(db_session):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(10):
        _job(db_session, created_at=t0 + timedelta(minutes=i), run_after=utcnow() + timedelta(hours=1))
    due_late = _job(db_session, created_at=t0 + timedelta(minutes=20))

    assert claim_next(db_session, "worker-a") is None
    db_session.refresh(due_late)
    assert due_late.status == JobStatus.QUEUED


# --- update_job_status_stmt ---------------------------------------------------


def test_update_stmt_moves_queued_to_running_with_worker_and_started_at(db_session):
    job = _job(db_session)

    result = db_session.execute(
        update_job_status_stmt(job.id, JobStatus.RUNNING, JobStatus.QUEUED, worker_id="w-1")
    )
    db_session.commit()
    db_session.refresh(job)

    assert result.rowcount == 1
    assert job.status == JobStatus.RUNNING
    assert job.worker_id == "w-1"
    assert job.started_at is not None


def test_update_stmt_sets_completed_at_on_terminal_target(db_session):
    job = _job(db_session, status=JobStatus.RUNNING)

    result = db_session.execute(update_job_status_stmt(job.id, JobStatus.COMPLETED, JobStatus.RUNNING))
    db_session.commit()
    db_session.refresh(job)

    assert result.rowcount == 1
    assert job.status == JobStatus.COMPLETED
    assert job.completed_at is not None


def test_update_stmt_is_a_noop_when_expected_status_does_not_match(db_session):
    job = _job(db_session)

    result = db_session.execute(update_job_status_stmt(job.id, JobStatus.RUNNING, JobStatus.CANCELLED))
    db_session.commit()
    db_session.refresh(job)

    assert result.rowcount == 0
    assert job.status == JobStatus.QUEUED
    assert job.started_at is None


# --- record_failure -----------------------------------------------------------


def test_record_failure_requeues_with_first_backoff_delay(db_session):
    job, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})
    before = utcnow()

    failed = record_failure(db_session, job, "boom")

    assert failed.status == JobStatus.QUEUED
    assert failed.retry_count == 1
    assert failed.stage == "retry 1 scheduled"
    assert failed.error == "boom"
    delay = RETRY_DELAYS_SECONDS[0]
    assert before + timedelta(seconds=delay - 5) < as_utc(failed.run_after) <= before + timedelta(
        seconds=delay + 5
    )


def test_record_failure_uses_longer_delay_on_second_retry(db_session):
    job, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})
    record_failure(db_session, job, "first")
    before = utcnow()

    failed = record_failure(db_session, job, "second")

    assert failed.status == JobStatus.QUEUED
    assert failed.retry_count == 2
    delay = RETRY_DELAYS_SECONDS[1]
    assert before + timedelta(seconds=delay - 5) < as_utc(failed.run_after) <= before + timedelta(
        seconds=delay + 5
    )


def test_record_failure_marks_failed_when_attempts_are_exhausted(db_session):
    job, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})
    record_failure(db_session, job, "first")
    record_failure(db_session, job, "second")

    failed = record_failure(db_session, job, "third")

    assert failed.status == JobStatus.FAILED
    assert failed.retry_count == MAX_ATTEMPTS
    assert failed.stage == "failed"
    assert failed.completed_at is not None


def test_record_failure_caps_retries_at_max_attempts_constant(db_session):
    job = _job(db_session, max_attempts=5)

    for attempt in range(1, 4):
        failed = record_failure(db_session, job, f"boom {attempt}")

    assert failed.status == JobStatus.FAILED
    assert failed.retry_count == MAX_ATTEMPTS


def test_record_failure_fails_immediately_when_max_attempts_is_one(db_session):
    job = _job(db_session, max_attempts=1)

    failed = record_failure(db_session, job, "boom")

    assert failed.status == JobStatus.FAILED
    assert failed.retry_count == 1


def test_record_failure_truncates_long_errors_to_two_thousand_chars(db_session):
    job, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})

    failed = record_failure(db_session, job, "x" * 2500)

    assert len(failed.error) == 2000


# --- request_cancel -----------------------------------------------------------


def test_request_cancel_cancels_a_queued_job(db_session):
    job, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})

    cancelled = request_cancel(db_session, job)

    assert cancelled.status == JobStatus.CANCELLED
    assert cancelled.completed_at is not None


def test_request_cancel_sets_flag_on_running_job(db_session):
    job, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})
    transition(db_session, job, JobStatus.RUNNING)

    flagged = request_cancel(db_session, job)

    assert flagged.status == JobStatus.RUNNING
    assert flagged.cancel_requested is True


def test_request_cancel_ignores_terminal_jobs(db_session):
    job, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})
    transition(db_session, job, JobStatus.RUNNING)
    transition(db_session, job, JobStatus.COMPLETED)

    unchanged = request_cancel(db_session, job)

    assert unchanged.status == JobStatus.COMPLETED
    assert unchanged.cancel_requested is False


# --- queue_depth ---------------------------------------------------------------


def test_queue_depth_is_empty_for_a_fresh_database(db_session):
    assert queue_depth(db_session) == {}


def test_queue_depth_counts_jobs_per_status(db_session):
    _job(db_session, status=JobStatus.QUEUED)
    _job(db_session, status=JobStatus.QUEUED)
    _job(db_session, status=JobStatus.RUNNING)
    _job(db_session, status=JobStatus.COMPLETED)

    assert queue_depth(db_session) == {
        JobStatus.QUEUED: 2,
        JobStatus.RUNNING: 1,
        JobStatus.COMPLETED: 1,
    }


# --- due_jobs -------------------------------------------------------------------


def test_due_jobs_returns_only_queued_and_due_rows(db_session):
    due = _job(db_session)
    _job(db_session, run_after=utcnow() + timedelta(hours=1))
    _job(db_session, status=JobStatus.RUNNING)

    result = due_jobs(db_session)

    assert [j.id for j in result] == [due.id]


def test_due_jobs_orders_by_priority_then_age(db_session):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    a = _job(db_session, priority=5, created_at=t0)
    b = _job(db_session, priority=1, created_at=t0 + timedelta(days=1))
    c = _job(db_session, priority=5, created_at=t0 + timedelta(days=2))

    result = due_jobs(db_session)

    assert [j.id for j in result] == [b.id, a.id, c.id]


def test_due_jobs_respects_limit(db_session):
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    jobs = [_job(db_session, created_at=t0 + timedelta(minutes=i)) for i in range(12)]

    result = due_jobs(db_session, limit=5)

    assert [j.id for j in result] == [j.id for j in jobs[:5]]


# --- as_utc ---------------------------------------------------------------------


def test_as_utc_attaches_utc_to_naive_datetimes():
    naive = datetime(2026, 11, 12, 10, 30)

    aware = as_utc(naive)

    assert aware.tzinfo is not None
    assert aware == datetime(2026, 11, 12, 10, 30, tzinfo=UTC)


def test_as_utc_leaves_aware_datetimes_alone():
    aware = datetime(2026, 11, 12, 10, 30, tzinfo=UTC)

    assert as_utc(aware) is aware
