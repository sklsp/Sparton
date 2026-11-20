"""API tests for the feature-flagged create domain (generation, datasets, training)."""

from __future__ import annotations

import tempfile

from app.generation.comfyui_client import ComfyUIError
from app.generation.service import WorkflowError, WorkflowInfo


class FakeComfyUI:
    """Stands in for ComfyUIService: no workflow files, no HTTP, no GPU.

    The routes under test call _get_comfyui() per request, so monkeypatching that
    factory to return this object keeps every branch reachable without a live
    ComfyUI or the experimental extras (docs/DECISIONS.md D-020).
    """

    def __init__(self) -> None:
        self.status_result = {"base_url": "http://comfyui.test", "connected": True}
        self.workflows = [WorkflowInfo(id="wf1", name="Test Workflow", node_count=3)]
        self.delete_result = True
        self.validate_result = {"valid": True, "checks": [], "errors": []}
        self.build_graph_result = ({"1": {"class_type": "KSampler"}}, 42)
        self.generated_entries = [
            {"filename": "img.png", "size_bytes": 9, "created_at": "2026-01-01T00:00:00"},
        ]
        self.image_bytes = b"png-bytes"
        self.calls: dict[str, object] = {}

    def status(self) -> dict:
        return self.status_result

    def list_workflows(self):
        return self.workflows

    def save_workflow(self, name, graph, description="", arch="generic", inputs=None):
        self.calls["save"] = (name, graph, description, arch, inputs)
        return WorkflowInfo(id=name.lower(), name=name)

    def delete_workflow(self, workflow_id):
        self.calls["delete"] = workflow_id
        return self.delete_result

    def validate_workflow_request(self, workflow_id, params):
        self.calls["validate"] = (workflow_id, dict(params))
        return self.validate_result

    def build_graph(self, workflow_id, params):
        self.calls["build"] = (workflow_id, dict(params))
        return self.build_graph_result

    def list_generated(self, limit=50):
        return [dict(entry) for entry in self.generated_entries]

    def read_generated(self, filename):
        self.calls["read"] = filename
        return self.image_bytes


def _use_fake(monkeypatch, fake: FakeComfyUI) -> None:
    monkeypatch.setattr("app.api.create._get_comfyui", lambda: fake)


def test_create_routes_exist_without_documents(create_domains_client):
    """A create route answers 401 without headers, not 404.

    A disabled domain has no routes at all (docs/DECISIONS.md D-020), so a 401
    proves the router was registered and only the auth dependency fired.
    """
    response = create_domains_client.get("/training/status")
    assert response.status_code == 401


def test_documents_routes_absent_from_create_client(create_domains_client):
    """The documents domain stays off in this client, so its routes do not exist."""
    response = create_domains_client.get("/documents")
    assert response.status_code == 404


# --------------------------------------------------------------------------
# ComfyUI status and workflow library (app/api/create.py)
# --------------------------------------------------------------------------
def test_comfyui_status_returns_service_status(create_domains_client, create_domain_headers, monkeypatch):
    fake = FakeComfyUI()
    _use_fake(monkeypatch, fake)
    response = create_domains_client.get("/comfyui/status", headers=create_domain_headers)
    assert response.status_code == 200
    assert response.json() == {"base_url": "http://comfyui.test", "connected": True}


def test_comfyui_status_maps_comfyui_error(create_domains_client, create_domain_headers, monkeypatch):
    fake = FakeComfyUI()

    def status():
        raise ComfyUIError("ComfyUI unreachable", status_code=502)

    fake.status = status
    _use_fake(monkeypatch, fake)
    response = create_domains_client.get("/comfyui/status", headers=create_domain_headers)
    assert response.status_code == 502
    assert response.json()["detail"] == "ComfyUI unreachable"


def test_list_workflows(create_domains_client, create_domain_headers, monkeypatch):
    fake = FakeComfyUI()
    _use_fake(monkeypatch, fake)
    response = create_domains_client.get("/comfyui/workflows", headers=create_domain_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["workflows"][0]["id"] == "wf1"
    assert body["workflows"][0]["name"] == "Test Workflow"


def test_import_workflow(create_domains_client, create_domain_headers, monkeypatch):
    fake = FakeComfyUI()
    _use_fake(monkeypatch, fake)
    response = create_domains_client.post(
        "/comfyui/workflows",
        json={"name": "My Flow", "graph": {"1": {"class_type": "KSampler"}}},
        headers=create_domain_headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == "my flow"
    assert body["name"] == "My Flow"
    name, graph = fake.calls["save"][0], fake.calls["save"][1]
    assert name == "My Flow"
    assert graph == {"1": {"class_type": "KSampler"}}


def test_import_workflow_rejects_bad_graph(create_domains_client, create_domain_headers, monkeypatch):
    fake = FakeComfyUI()

    def save_workflow(name, graph, description="", arch="generic", inputs=None):
        raise WorkflowError("Workflow must be a non-empty JSON object")

    fake.save_workflow = save_workflow
    _use_fake(monkeypatch, fake)
    response = create_domains_client.post(
        "/comfyui/workflows",
        json={"name": "My Flow", "graph": {}},
        headers=create_domain_headers,
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Workflow must be a non-empty JSON object"


def test_delete_workflow(create_domains_client, create_domain_headers, monkeypatch):
    fake = FakeComfyUI()
    _use_fake(monkeypatch, fake)
    response = create_domains_client.delete("/comfyui/workflows/wf1", headers=create_domain_headers)
    assert response.status_code == 200
    assert response.json() == {"deleted": True}
    assert fake.calls["delete"] == "wf1"


def test_delete_missing_workflow_404(create_domains_client, create_domain_headers, monkeypatch):
    fake = FakeComfyUI()
    fake.delete_result = False
    _use_fake(monkeypatch, fake)
    response = create_domains_client.delete("/comfyui/workflows/nope", headers=create_domain_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "Workflow not found"


# --------------------------------------------------------------------------
# Generation (app/api/create.py)
# --------------------------------------------------------------------------
def test_validate_generation_passes_non_null_params(create_domains_client, create_domain_headers, monkeypatch):
    fake = FakeComfyUI()
    _use_fake(monkeypatch, fake)
    response = create_domains_client.post(
        "/comfyui/validate-generation",
        json={"workflow_id": "wf1", "prompt": "a cat", "seed": 7},
        headers=create_domain_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"valid": True, "checks": [], "errors": []}
    workflow_id, params = fake.calls["validate"]
    assert workflow_id == "wf1"
    assert params == {"workflow_id": "wf1", "prompt": "a cat", "seed": 7}


def test_generate_queues_a_job(create_domains_client, create_domain_headers, monkeypatch):
    fake = FakeComfyUI()
    _use_fake(monkeypatch, fake)
    enqueued: dict[str, object] = {}

    class Job:
        id = 7
        status = "QUEUED"

    def enqueue(db, **kwargs):
        enqueued.update(kwargs)
        return Job(), True

    monkeypatch.setattr("app.api.create.enqueue", enqueue)
    response = create_domains_client.post(
        "/comfyui/generate",
        json={"workflow_id": "wf1", "prompt": "a cat"},
        headers=create_domain_headers,
    )
    assert response.status_code == 202
    body = response.json()
    assert body["job_id"] == 7
    assert body["seed"] == 42
    assert body["status"] == "QUEUED"
    assert enqueued["type"] == "comfy_generation"
    payload = enqueued["payload"]
    assert payload["workflow_id"] == "wf1"
    assert payload["prompt"] == "a cat"
    assert payload["seed"] == 42
    assert str(enqueued["idempotency_key"]).startswith("gen:")


def test_generate_unknown_workflow_404(create_domains_client, create_domain_headers, monkeypatch):
    fake = FakeComfyUI()

    def build_graph(workflow_id, params):
        raise WorkflowError("Workflow 'nope' not found", status_code=404)

    fake.build_graph = build_graph
    _use_fake(monkeypatch, fake)
    response = create_domains_client.post(
        "/comfyui/generate",
        json={"workflow_id": "nope", "prompt": "a cat"},
        headers=create_domain_headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Workflow 'nope' not found"


def test_list_generated_maps_db_rows(create_domains_client, create_domain_headers, db_session, monkeypatch):
    from sqlalchemy import select

    from app.core.database.creation_models import GeneratedImage
    from app.core.database.identity import User

    fake = FakeComfyUI()
    _use_fake(monkeypatch, fake)
    user = db_session.execute(select(User).where(User.email == "admin@example.com")).scalar_one()
    row = GeneratedImage(filename="img.png", organization_id=user.organization_id)
    db_session.add(row)
    db_session.commit()

    response = create_domains_client.get("/generated", headers=create_domain_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    image = body["images"][0]
    assert image["filename"] == "img.png"
    assert image["id"] == row.id
    assert image["url"] == "/generated/img.png"


def test_read_generated_serves_bytes(create_domains_client, create_domain_headers, monkeypatch, tmp_path):
    fake = FakeComfyUI()
    _use_fake(monkeypatch, fake)
    # The route stages the bytes in the OS temp dir; point that at tmp_path.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    response = create_domains_client.get("/generated/img.png", headers=create_domain_headers)
    assert response.status_code == 200
    assert response.content == b"png-bytes"
    assert response.headers["content-type"].startswith("image/png")
    assert fake.calls["read"] == "img.png"


def test_read_missing_generated_404(create_domains_client, create_domain_headers, monkeypatch):
    fake = FakeComfyUI()

    def read_generated(filename):
        raise WorkflowError("Image 'missing.png' not found", status_code=404)

    fake.read_generated = read_generated
    _use_fake(monkeypatch, fake)
    response = create_domains_client.get("/generated/missing.png", headers=create_domain_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "Image 'missing.png' not found"


def test_generation_routes_require_auth(create_domains_client):
    """Every generation route answers 401 without headers, not 404."""
    client = create_domains_client
    assert client.get("/comfyui/status").status_code == 401
    assert client.get("/comfyui/workflows").status_code == 401
    assert client.post("/comfyui/workflows", json={"name": "x", "graph": {}}).status_code == 401
    assert client.delete("/comfyui/workflows/wf1").status_code == 401
    assert client.post("/comfyui/validate-generation", json={}).status_code == 401
    assert client.post("/comfyui/generate", json={}).status_code == 401
    assert client.get("/generated").status_code == 401
    assert client.get("/generated/img.png").status_code == 401
