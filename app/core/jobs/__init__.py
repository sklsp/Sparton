"""Durable background jobs (Ares foundation, generalized for SPARTON).

Jobs live in the database, so a job submitted by one API process can be
executed by any worker process. The state machine below is the single source
of truth for legal transitions; workers claim rows atomically so two workers
never run the same job.

All long-running work uses this one system: document ingestion, RAG
indexing, research, crawling, dataset validation, captioning, LoRA training,
ComfyUI generation and reports.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# Legal transitions. Anything not listed here is rejected, which prevents
# COMPLETED -> RUNNING, FAILED -> COMPLETED and CANCELLED -> RUNNING.
TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.COMPLETED: set(),
    JobStatus.FAILED: {JobStatus.QUEUED},  # explicit retry only
    JobStatus.CANCELLED: set(),
}


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    return target in TRANSITIONS.get(current, set())


class InvalidTransition(RuntimeError):
    pass


# Retry policy shared by all job types.
MAX_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (30, 120)
