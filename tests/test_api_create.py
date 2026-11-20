"""API tests for the feature-flagged create domain (generation, datasets, training)."""

from __future__ import annotations

import base64
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


# --------------------------------------------------------------------------
# Datasets (app/api/create.py) — projects, images, captions, validation
# --------------------------------------------------------------------------

# A real 1x1 red PNG. The tests must pass with or without Pillow installed:
# without it, dimension detection degrades to 0x0 and validation marks the image
# invalid; with it, the image decodes fine. Assertions below avoid depending on
# either outcome (Pillow is an experimental extra, not in requirements.txt).
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGM4IScHAAK2AQU0pnWq"
    "AAAAAElFTkSuQmCC"
)


def _use_tmp_dataset_service(monkeypatch, tmp_path):
    """Point the dataset service at a temp dir so tests never write to ./data/loras."""
    from app.datasets.service import DatasetService

    monkeypatch.setattr(
        "app.api.create.get_dataset_service",
        lambda: DatasetService(str(tmp_path / "datasets")),
    )


def _upload_image(client, headers, dataset_id, filename="img1.png", content=_PNG_1X1):
    return client.post(
        f"/datasets/{dataset_id}/images",
        files={"file": (filename, content, "image/png")},
        headers=headers,
    )


def test_dataset_crud_lifecycle(create_domains_client, create_domain_headers, monkeypatch, tmp_path):
    """Create -> upload -> caption -> delete image -> delete dataset, with disk checks."""
    _use_tmp_dataset_service(monkeypatch, tmp_path)
    client = create_domains_client

    # Auth dependency fires before routing: 401, not 404.
    assert client.get("/datasets").status_code == 401

    created = client.post(
        "/datasets",
        json={"name": "Hero Set", "description": "product shots", "trigger_word": "zephyr"},
        headers=create_domain_headers,
    )
    assert created.status_code == 201
    dataset_id = created.json()["id"]
    assert created.json()["name"] == "Hero Set"

    upload = _upload_image(client, create_domain_headers, dataset_id)
    assert upload.status_code == 201
    image_id = upload.json()["id"]
    assert upload.json()["filename"] == "img1.png"

    # Files land under <root>/dataset_<id>/ with an empty .txt sidecar.
    data_dir = tmp_path / "datasets" / f"dataset_{dataset_id}"
    assert (data_dir / "img1.png").is_file()
    assert (data_dir / "img1.txt").read_text(encoding="utf-8") == ""

    # A duplicate filename is deduplicated with a _1 suffix.
    dup = _upload_image(client, create_domain_headers, dataset_id)
    assert dup.status_code == 201
    assert dup.json()["filename"] == "img1_1.png"

    images = client.get(f"/datasets/{dataset_id}/images", headers=create_domain_headers)
    assert images.status_code == 200
    body = images.json()
    assert body["count"] == 2
    first = next(i for i in body["images"] if i["id"] == image_id)
    assert first["filename"] == "img1.png"
    assert isinstance(first["width"], int) and isinstance(first["height"], int)

    captioned = client.put(
        f"/datasets/{dataset_id}/images/{image_id}/caption",
        json={"caption": "  a red car, parked  "},
        headers=create_domain_headers,
    )
    assert captioned.status_code == 200
    assert captioned.json()["caption"] == "a red car, parked"
    assert (data_dir / "img1.txt").read_text(encoding="utf-8") == "a red car, parked"

    deleted = client.delete(
        f"/datasets/{dataset_id}/images/{image_id}", headers=create_domain_headers
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": True}
    assert not (data_dir / "img1.png").exists() and not (data_dir / "img1.txt").exists()

    listing = client.get("/datasets", headers=create_domain_headers)
    assert listing.status_code == 200
    names = [d["name"] for d in listing.json()["datasets"]]
    assert "Hero Set" in names

    gone = client.delete(f"/datasets/{dataset_id}", headers=create_domain_headers)
    assert gone.status_code == 200
    assert gone.json() == {"deleted": True}
    assert not data_dir.exists()


def test_upload_rejects_bad_extension_empty_and_oversize(
    create_domains_client, create_domain_headers, monkeypatch, tmp_path
):
    _use_tmp_dataset_service(monkeypatch, tmp_path)
    client = create_domains_client
    dataset_id = client.post("/datasets", json={"name": "D"}, headers=create_domain_headers).json()["id"]

    bad_ext = client.post(
        f"/datasets/{dataset_id}/images",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        headers=create_domain_headers,
    )
    assert bad_ext.status_code == 400
    assert bad_ext.json()["detail"] == "Unsupported image type; allowed: .png, .jpg, .jpeg, .webp"

    empty = _upload_image(client, create_domain_headers, dataset_id, content=b"")
    assert empty.status_code == 400
    assert empty.json()["detail"] == "Empty image"

    from app.core.config import settings

    # ~52 bytes; the 66-byte PNG below exceeds it. max_image_upload_bytes is a
    # computed property, so patch the underlying mb field instead.
    monkeypatch.setattr(settings, "max_image_upload_mb", 0.00005)
    oversize = _upload_image(client, create_domain_headers, dataset_id)
    assert oversize.status_code == 413
    assert "MB limit" in oversize.json()["detail"]


def test_dataset_scoping_across_orgs(create_domains_client, create_domain_headers, monkeypatch, tmp_path):
    """Datasets are org-scoped: a second organization cannot see or touch them."""
    _use_tmp_dataset_service(monkeypatch, tmp_path)
    client = create_domains_client

    dataset_id = client.post("/datasets", json={"name": "A only"}, headers=create_domain_headers).json()["id"]
    image_id = _upload_image(client, create_domain_headers, dataset_id).json()["id"]

    registered = client.post(
        "/auth/register",
        json={
            "email": "user-b@example.com",
            "password": "long-password-2",
            "organization_name": "Org B",
        },
    )
    assert registered.status_code == 201
    headers_b = {"Authorization": f"Bearer {registered.json()['token']}"}

    listing = client.get("/datasets", headers=headers_b)
    assert listing.status_code == 200
    assert dataset_id not in [d["id"] for d in listing.json()["datasets"]]

    assert client.get(f"/datasets/{dataset_id}/images", headers=headers_b).status_code == 404
    assert _upload_image(client, headers_b, dataset_id).status_code == 404
    assert (
        client.put(
            f"/datasets/{dataset_id}/images/{image_id}/caption",
            json={"caption": "sneak"},
            headers=headers_b,
        ).status_code
        == 404
    )
    # Deleting someone else's dataset is a no-op, not an error.
    assert client.delete(f"/datasets/{dataset_id}", headers=headers_b).json() == {"deleted": False}


def test_validate_reports_missing_captions_and_findings(
    create_domains_client, create_domain_headers, monkeypatch, tmp_path
):
    _use_tmp_dataset_service(monkeypatch, tmp_path)
    client = create_domains_client
    dataset_id = client.post("/datasets", json={"name": "V"}, headers=create_domain_headers).json()["id"]
    image_a = _upload_image(client, create_domain_headers, dataset_id, filename="a.png").json()
    _upload_image(client, create_domain_headers, dataset_id, filename="b.png")

    report = client.get(f"/datasets/{dataset_id}/validate", headers=create_domain_headers)
    assert report.status_code == 200
    body = report.json()
    assert body["total_images"] == 2
    assert body["captioned"] == 0
    assert body["missing_captions"] == 2
    assert body["issue_count"] >= 2
    assert body["score"] < 100
    kinds = {f["kind"] for f in body["findings"]}
    assert "missing_caption" in kinds
    assert all(set(f) == {"image_id", "filename", "kind", "detail"} for f in body["findings"])

    # Captioning one image drops the missing-caption count.
    client.put(
        f"/datasets/{dataset_id}/images/{image_a['id']}/caption",
        json={"caption": "a car"},
        headers=create_domain_headers,
    )
    again = client.get(f"/datasets/{dataset_id}/validate", headers=create_domain_headers).json()
    assert again["missing_captions"] == 1
    assert again["captioned"] == 1


class _VisionProvider:
    """Minimal stand-in exposing only what generate_captions calls."""

    name = "fake-vision"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def is_available(self) -> bool:
        return True

    def vision_complete(self, prompt: str, image_b64: str) -> str:
        self.calls.append((prompt, image_b64))
        return "red car, parked on street"


def test_generate_captions_with_vision_provider(
    create_domains_client, create_domain_headers, monkeypatch, tmp_path
):
    """Vision captions are stored with the trigger word prefixed; human captions win."""
    from app.datasets.service import CAPTION_PROMPT

    _use_tmp_dataset_service(monkeypatch, tmp_path)
    fake = _VisionProvider()
    monkeypatch.setattr("app.datasets.service.get_llm_provider", lambda: fake)
    client = create_domains_client

    dataset_id = client.post(
        "/datasets", json={"name": "C", "trigger_word": "zephyr"}, headers=create_domain_headers
    ).json()["id"]
    image_id = _upload_image(client, create_domain_headers, dataset_id).json()["id"]

    result = client.post(f"/datasets/{dataset_id}/captions", json={}, headers=create_domain_headers)
    assert result.status_code == 200
    body = result.json()
    assert body["captioned"] == 1
    entry = body["results"][0]
    assert entry["id"] == image_id and "error" not in entry
    expected_caption = "zephyr, red car, parked on street"
    assert entry["caption"] == expected_caption

    # The provider saw the caption prompt and the raw image bytes as base64.
    assert fake.calls[0][0] == CAPTION_PROMPT
    assert fake.calls[0][1] == base64.b64encode(_PNG_1X1).decode()

    data_dir = tmp_path / "datasets" / f"dataset_{dataset_id}"
    assert (data_dir / "img1.txt").read_text(encoding="utf-8") == expected_caption
    listed = client.get(f"/datasets/{dataset_id}/images", headers=create_domain_headers).json()
    assert listed["images"][0]["caption"] == expected_caption

    # A human caption is not overwritten unless overwrite=true.
    client.put(
        f"/datasets/{dataset_id}/images/{image_id}/caption",
        json={"caption": "my own words"},
        headers=create_domain_headers,
    )
    skipped = client.post(f"/datasets/{dataset_id}/captions", json={}, headers=create_domain_headers)
    assert skipped.json() == {"captioned": 0, "results": []}

    forced = client.post(
        f"/datasets/{dataset_id}/captions", json={"overwrite": True}, headers=create_domain_headers
    )
    assert forced.json()["captioned"] == 1


def test_generate_captions_llm_error_per_image(
    create_domains_client, create_domain_headers, monkeypatch, tmp_path
):
    """The deterministic test provider has no vision: errors are per-image, not fatal."""
    _use_tmp_dataset_service(monkeypatch, tmp_path)
    client = create_domains_client
    dataset_id = client.post("/datasets", json={"name": "E"}, headers=create_domain_headers).json()["id"]
    image_id = _upload_image(client, create_domain_headers, dataset_id).json()["id"]

    result = client.post(f"/datasets/{dataset_id}/captions", json={}, headers=create_domain_headers)
    assert result.status_code == 200
    body = result.json()
    assert body["captioned"] == 0
    entry = body["results"][0]
    assert entry["id"] == image_id
    assert "caption" not in entry
    assert entry["error"] == "test provider does not support vision"


def test_dataset_routes_404_for_unknown_ids(create_domains_client, create_domain_headers, monkeypatch, tmp_path):
    _use_tmp_dataset_service(monkeypatch, tmp_path)
    client = create_domains_client

    assert client.get("/datasets/999/images", headers=create_domain_headers).status_code == 404
    assert (
        client.post("/datasets/999/captions", json={}, headers=create_domain_headers).status_code == 404
    )
    validate = client.get("/datasets/999/validate", headers=create_domain_headers)
    assert validate.status_code == 404
    assert validate.json()["detail"] == "Dataset not found"
    # Deleting a missing dataset is a no-op, not an error.
    assert client.delete("/datasets/999", headers=create_domain_headers).json() == {"deleted": False}
