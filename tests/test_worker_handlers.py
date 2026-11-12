"""Worker handlers (workers.handlers): research and ComfyUI generation drive Job rows."""

import pytest

from app.core.database.creation_models import GeneratedImage
from app.core.database.domain_models import Job, JobStatus
from app.core.jobs import transport
from app.generation import comfyui_client as comfy_client_module, service as generation_service_module
from workers import handlers


def _make_job(db_session, **fields) -> int:
    """Insert a committed Job row and return its id."""
    job = Job(type=fields.pop("type", "research"), **fields)
    db_session.add(job)
    db_session.commit()
    return job.id


# --- register_all ---------------------------------------------------------------


def test_register_all_registers_every_job_type(monkeypatch):
    monkeypatch.setattr(transport, "_handlers", {})

    handlers.register_all()

    for job_type in (
        "research",
        "comfy_generation",
        "crawl_shop",
        "crawl_competitor",
        "generate_report",
        "schedule_crawls",
    ):
        assert transport.get_handler(job_type) is not None
    assert transport.get_handler("research") is handlers._handle_research
    assert transport.get_handler("comfy_generation") is handlers._handle_comfy_generation


# --- research handler -----------------------------------------------------------


def test_handle_research_runs_investigation_with_start_urls(db_session, monkeypatch):
    job_id = _make_job(db_session)
    captured = {}

    def fake_investigation(db, job, *, start_urls=None):
        captured["job_id"] = job.id
        captured["start_urls"] = start_urls

    monkeypatch.setattr("app.research.intelligence.run_investigation", fake_investigation)

    handlers._handle_research({"job_id": str(job_id), "start_urls": ["https://a.example", "https://b.example"]})

    assert captured == {"job_id": job_id, "start_urls": ["https://a.example", "https://b.example"]}


@pytest.mark.parametrize("payload_extra", [{}, {"start_urls": []}])
def test_handle_research_passes_none_when_start_urls_absent_or_empty(db_session, monkeypatch, payload_extra):
    """An absent or empty start_urls list must reach run_investigation as None."""
    job_id = _make_job(db_session)
    captured = {}

    def fake_investigation(db, job, *, start_urls=None):
        captured["start_urls"] = start_urls

    monkeypatch.setattr("app.research.intelligence.run_investigation", fake_investigation)

    handlers._handle_research({"job_id": str(job_id), **payload_extra})

    assert captured == {"start_urls": None}


def test_handle_research_ignores_missing_job(db_session, monkeypatch):
    called = []

    def fake_investigation(db, job, *, start_urls=None):
        called.append(job.id)

    monkeypatch.setattr("app.research.intelligence.run_investigation", fake_investigation)

    handlers._handle_research({"job_id": "999"})

    assert called == []


@pytest.mark.parametrize("status", [JobStatus.COMPLETED.value, JobStatus.CANCELLED.value])
def test_handle_research_skips_finished_jobs(db_session, monkeypatch, status):
    job_id = _make_job(db_session, status=status)
    called = []

    def fake_investigation(db, job, *, start_urls=None):
        called.append(job.id)

    monkeypatch.setattr("app.research.intelligence.run_investigation", fake_investigation)

    handlers._handle_research({"job_id": str(job_id)})

    assert called == []


# --- ComfyUI generation handler ---------------------------------------------------


class FakeComfyUIError(Exception):
    """Stand-in for app.generation.comfyui_client.ComfyUIError with a detail."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class FakeComfyUIService:
    """In-memory ComfyUIService: records calls and reports progress, never touches the network."""

    def __init__(self, saved=(), error=None):
        self.saved = list(saved)
        self.error = error
        self.calls = []
        self.progress_calls = []
        self.cancelled_result = None
        self.provenance = None

    def run_generation_sync(self, workflow_id, params, progress=None, cancelled=None):
        self.calls.append({"workflow_id": workflow_id, "params": params})
        if progress is not None:
            progress(0.5, "halfway")
            self.progress_calls.append((0.5, "halfway"))
        if cancelled is not None:
            self.cancelled_result = cancelled()
        if self.error is not None:
            raise self.error
        return list(self.saved)

    def write_provenance(self, saved, meta):
        self.provenance = (list(saved), dict(meta))


@pytest.fixture()
def comfy(monkeypatch):
    """Point the handler at a fake service and error class so real ComfyUI is never reached."""
    service = FakeComfyUIService()
    monkeypatch.setattr(generation_service_module, "ComfyUIService", lambda: service)
    monkeypatch.setattr(comfy_client_module, "ComfyUIError", FakeComfyUIError)
    return service


def _comfy_payload(job_id, **extra):
    payload = {"job_id": str(job_id), "workflow_id": "wf-test", "params": {"steps": 4}, "prompt": "a cat"}
    payload.update(extra)
    return payload


def test_comfy_generation_completes_job_and_records_images(db_session, comfy):
    job_id = _make_job(db_session, type="comfy_generation")
    comfy.saved = ["img_0001.png", "img_0002.png"]

    handlers._handle_comfy_generation(_comfy_payload(job_id, seed=42))

    db_session.expire_all()
    job = db_session.get(Job, job_id)
    assert job.status == JobStatus.COMPLETED.value
    assert job.stage == "complete"
    assert job.progress == 1.0
    assert job.message == "halfway"  # the progress callback committed mid-run state
    assert job.started_at is not None
    assert job.completed_at is not None
    assert job.outputs == [{"filename": "img_0001.png"}, {"filename": "img_0002.png"}]

    images = (
        db_session.query(GeneratedImage)
        .filter_by(job_id=job_id)
        .order_by(GeneratedImage.id)
        .all()
    )
    assert [image.filename for image in images] == ["img_0001.png", "img_0002.png"]
    for image in images:
        assert image.workflow_slug == "wf-test"
        assert image.prompt == "a cat"
        assert image.seed == 42

    assert comfy.calls == [{"workflow_id": "wf-test", "params": {"steps": 4}}]
    saved, meta = comfy.provenance
    assert saved == ["img_0001.png", "img_0002.png"]
    assert meta["job_id"] == job_id
    assert meta["workflow_id"] == "wf-test"
    assert meta["seed"] == 42


def test_comfy_generation_polls_cancellation_flag(db_session, comfy):
    job_id = _make_job(db_session, type="comfy_generation")

    handlers._handle_comfy_generation(_comfy_payload(job_id))

    # Job.cancel_requested defaults to false, so the callback must report not cancelled.
    assert comfy.cancelled_result is False


def test_comfy_generation_records_failure_and_reraises(db_session, comfy):
    job_id = _make_job(db_session, type="comfy_generation")
    comfy.error = FakeComfyUIError("boom")

    with pytest.raises(FakeComfyUIError):
        handlers._handle_comfy_generation(_comfy_payload(job_id))

    db_session.expire_all()
    job = db_session.get(Job, job_id)
    assert job.status == JobStatus.FAILED.value
    assert job.error == "boom"
    assert job.completed_at is not None


def test_comfy_generation_ignores_missing_job(db_session, comfy):
    handlers._handle_comfy_generation(_comfy_payload(999))

    assert comfy.calls == []


@pytest.mark.parametrize("status", [JobStatus.COMPLETED.value, JobStatus.CANCELLED.value])
def test_comfy_generation_skips_finished_jobs(db_session, comfy, status):
    job_id = _make_job(db_session, type="comfy_generation", status=status)

    handlers._handle_comfy_generation(_comfy_payload(job_id))

    assert comfy.calls == []
    db_session.expire_all()
    assert db_session.get(Job, job_id).status == status
