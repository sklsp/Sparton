"""Job state machine: an illegal transition raises InvalidTransition (not a NameError)."""

import pytest

from app.core.database.domain_models import JobStatus
from app.core.jobs import InvalidTransition
from app.core.jobs.queue import enqueue, transition


def test_legal_transitions_set_the_timestamps(db_session):
    job, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 1})
    transition(db_session, job, JobStatus.RUNNING)
    assert job.started_at is not None
    transition(db_session, job, JobStatus.COMPLETED)
    assert job.completed_at is not None


def test_an_illegal_transition_raises_invalid_transition(db_session):
    job, _ = enqueue(db_session, type="crawl_shop", payload={"shop_id": 2})
    transition(db_session, job, JobStatus.RUNNING)
    transition(db_session, job, JobStatus.COMPLETED)
    with pytest.raises(InvalidTransition):
        transition(db_session, job, JobStatus.RUNNING)  # COMPLETED -> RUNNING is not allowed
