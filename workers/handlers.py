"""Job handler registry: connects domain work to the shared queue.

Each handler opens its own DB session (workers are separate processes) and
drives the durable Job row through its state machine.
"""

from __future__ import annotations

import logging

from app.core.database.base import SessionLocal
from app.core.database.domain_models import Job, JobStatus
from app.core.jobs.transport import register_handler

logger = logging.getLogger(__name__)


def _handle_research(payload: dict) -> None:
    """Run one market-intelligence investigation."""
    from app.research.intelligence import run_investigation

    job_id = payload.get("job_id")
    db = SessionLocal()
    try:
        job = db.get(Job, int(job_id))
        if job is None or job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED):
            return
        run_investigation(db, job, start_urls=payload.get("start_urls") or None)
    finally:
        db.close()


def _handle_comfy_generation(payload: dict) -> None:
    """Run one ComfyUI generation and record the produced images."""
    from app.core.database.creation_models import GeneratedImage
    from app.core.database.models import utcnow
    from app.generation.comfyui_client import ComfyUIError
    from app.generation.service import ComfyUIService

    job_id = payload.get("job_id")
    db = SessionLocal()
    service = ComfyUIService()
    try:
        job = db.get(Job, int(job_id))
        if job is None or job.status in (JobStatus.COMPLETED, JobStatus.CANCELLED):
            return
        job.status = JobStatus.RUNNING.value
        job.stage = "generating"
        job.started_at = utcnow()
        db.commit()

        try:
            saved = service.run_generation_sync(
                payload["workflow_id"],
                payload.get("params", {}),
                progress=lambda frac, msg: setattr(job, "progress", frac)
                or setattr(job, "message", msg) or db.commit(),
                cancelled=lambda: db.execute(
                    __import__("sqlalchemy").select(Job.cancel_requested).where(Job.id == job.id)
                ).scalar(),
            )
        except ComfyUIError as exc:
            job.status = JobStatus.FAILED.value
            job.error = exc.detail
            job.completed_at = utcnow()
            db.commit()
            raise

        for filename in saved:
            db.add(GeneratedImage(
                organization_id=job.organization_id,
                project_id=job.project_id,
                job_id=job.id,
                workflow_slug=payload.get("workflow_id", ""),
                prompt=payload.get("prompt", ""),
                filename=filename,
                seed=payload.get("seed"),
                lora_name=payload.get("lora_name"),
                created_by=job.created_by,
            ))
        service.write_provenance(saved, {
            "job_id": job.id,
            "workflow_id": payload.get("workflow_id"),
            "prompt": payload.get("prompt", ""),
            "seed": payload.get("seed"),
            "lora_name": payload.get("lora_name"),
        })
        job.status = JobStatus.COMPLETED.value
        job.stage = "complete"
        job.progress = 1.0
        job.outputs = [{"filename": f} for f in saved]
        job.completed_at = utcnow()
        db.commit()
    finally:
        db.close()


def register_all() -> None:
    register_handler("research", _handle_research)
    register_handler("comfy_generation", _handle_comfy_generation)

    # Sparton Intelligence — the product. Imported lazily so a deployment with
    # the domain disabled never pays for the import.
    from app.ecommerce.jobs import (
        crawl_one_competitor,
        crawl_shop,
        generate_report_job,
        schedule_due_crawls,
    )

    register_handler("crawl_shop", crawl_shop)
    register_handler("crawl_competitor", crawl_one_competitor)
    register_handler("generate_report", generate_report_job)
    register_handler("schedule_crawls", schedule_due_crawls)


# Register on import so both the worker process and the embedded worker get them.
register_all()
