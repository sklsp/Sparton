"""Unit tests for app/generation/service.py (Apollo's ComfyUI orchestration layer).

The service is tested against a fake ComfyUI client, so no server, network or
database is needed: workflow library CRUD, graph validation/mapping/injection,
the synchronous generation loop (success, cancel, error, timeout), generated
file bookkeeping and the pre-flight request validator.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.generation.comfyui_client import ComfyUIError
from app.generation.service import (
    ComfyUIService,
    WorkflowError,
    WorkflowInfo,
    _first_error,
    _read_json,
    _write_json_atomic,
    infer_mapping,
    inject_inputs,
    validate_graph,
)


def make_graph() -> dict[str, Any]:
    """A minimal valid API-format ComfyUI graph (checkpoint + sampler + text)."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "v1-5-pruned.ckpt"}},
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 42,
                "steps": 20,
                "cfg": 7.0,
                "positive": ["6", 0],
                "negative": ["7", 0],
            },
        },
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    }


def save_test_workflow(service: ComfyUIService, name: str = "Test WF") -> WorkflowInfo:
    return service.save_workflow(name, make_graph())


def save_lora_workflow(service: ComfyUIService, name: str = "lora_wf") -> WorkflowInfo:
    graph = make_graph()
    graph["8"] = {
        "class_type": "LoraLoader",
        "inputs": {"lora_name": "style.safetensors", "strength_model": 0.9, "strength_clip": 0.7},
    }
    inputs: dict[str, Any] = {
        "seed": {"node": "3", "field": "seed"},
        "prompt": {"node": "6", "field": "text"},
        "checkpoint": {"node": "1", "field": "ckpt_name"},
        "lora_name": {"node": "8", "field": "lora_name"},
        "lora_strength_model": {"node": "8", "field": "strength_model"},
        "lora_strength_clip": {"node": "8", "field": "strength_clip"},
    }
    return service.save_workflow(name, graph, inputs=inputs)


class FakeComfyUIClient:
    """Just enough of ComfyUIClient for the service to run against."""

    def __init__(self) -> None:
        self.base_url = "http://comfy.test"
        self.client_id = "fake-client"
        self.ping_result: dict[str, Any] | Exception = {}
        self.checkpoints: list[str] = ["v1-5-pruned.ckpt"]
        self.loras: list[str] = ["style.safetensors"]
        self.models_error: ComfyUIError | None = None
        # prompt_id -> sequence of history records; each poll pops the first one,
        # and once only a single record remains it is returned on every poll.
        self.history_records: dict[str, list[dict[str, Any]]] = {}
        self.view_results: dict[str, bytes] = {}
        self.view_errors: set[str] = set()
        self.view_calls: list[tuple[str, str, str]] = []
        self.queued_graphs: list[dict[str, Any]] = []
        self.history_calls = 0

    def ping(self) -> dict[str, Any]:
        if isinstance(self.ping_result, Exception):
            raise self.ping_result
        return self.ping_result

    def queue_prompt(self, graph: dict[str, Any]) -> str:
        self.queued_graphs.append(graph)
        return "pid-1"

    def history(self, prompt_id: str) -> dict[str, Any]:
        self.history_calls += 1
        seq = self.history_records.get(prompt_id) or []
        if not seq:
            return {}
        if len(seq) == 1:
            return seq[0]
        return seq.pop(0)

    def view_image(self, filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
        self.view_calls.append((filename, subfolder, folder_type))
        if filename in self.view_errors:
            raise ComfyUIError(f"Could not fetch {filename}", detail="404 from comfy")
        return self.view_results.get(filename, b"default-bytes")

    def list_checkpoints(self) -> list[str]:
        if self.models_error is not None:
            raise self.models_error
        return self.checkpoints

    def list_loras(self) -> list[str]:
        if self.models_error is not None:
            raise self.models_error
        return self.loras


@pytest.fixture()
def fake_client() -> FakeComfyUIClient:
    return FakeComfyUIClient()


@pytest.fixture()
def service(tmp_path: Path, fake_client: FakeComfyUIClient) -> ComfyUIService:
    # Explicit directories keep the suite away from the real workflows/ folder.
    return ComfyUIService(
        client=fake_client,  # type: ignore[arg-type]  # duck-typed fake; see FakeComfyUIClient
        workflow_dir=str(tmp_path / "workflows"),
        output_dir=str(tmp_path / "outputs"),
    )


class TestTypesAndJsonHelpers:
    def test_workflow_error_keeps_message_and_status(self) -> None:
        err = WorkflowError("boom", status_code=409)
        assert str(err) == "boom"
        assert err.message == "boom"
        assert err.status_code == 409

    def test_workflow_error_defaults_to_400(self) -> None:
        assert WorkflowError("x").status_code == 400

    def test_workflow_info_supported_inputs_and_dict(self) -> None:
        info = WorkflowInfo(id="w", name="W", inputs={"b": {}, "a": {}})
        assert info.supported_inputs == ["a", "b"]
        data = info.to_dict()
        assert data["id"] == "w"
        assert data["supported_inputs"] == ["a", "b"]
        assert data["node_count"] == 0

    def test_read_json_missing_and_corrupt(self, tmp_path: Path) -> None:
        assert _read_json(tmp_path / "nope.json") is None
        bad = tmp_path / "bad.json"
        bad.write_text("{", encoding="utf-8")
        assert _read_json(bad, default=7) == 7

    def test_write_json_atomic_roundtrip_leaves_no_temp_files(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "data.json"
        _write_json_atomic(target, {"a": [1, 2]})
        assert json.loads(target.read_text(encoding="utf-8")) == {"a": [1, 2]}
        leftovers = list(tmp_path.glob("*.tmp")) + list((tmp_path / "sub").glob("*.tmp"))
        assert leftovers == []


class TestValidateGraph:
    def test_rejects_non_dict_and_empty(self) -> None:
        with pytest.raises(WorkflowError, match="non-empty JSON object"):
            validate_graph([])
        with pytest.raises(WorkflowError):
            validate_graph({})

    def test_rejects_ui_format(self) -> None:
        with pytest.raises(WorkflowError, match="UI-format"):
            validate_graph({"nodes": [1, 2]})

    def test_rejects_node_that_is_not_an_object(self) -> None:
        with pytest.raises(WorkflowError, match="not an object"):
            validate_graph({"1": 5})

    def test_rejects_missing_class_type(self) -> None:
        with pytest.raises(WorkflowError, match="class_type"):
            validate_graph({"1": {"inputs": {}}})

    def test_rejects_missing_inputs_object(self) -> None:
        with pytest.raises(WorkflowError, match="'inputs' object"):
            validate_graph({"1": {"class_type": "KSampler"}})

    def test_accepts_valid_graph(self) -> None:
        graph = make_graph()
        assert validate_graph(graph) == graph


class TestInferMapping:
    def test_maps_sampler_latent_checkpoint_and_text(self) -> None:
        mapping = infer_mapping(make_graph())
        assert mapping["seed"] == {"node": "3", "field": "seed"}
        assert mapping["steps"] == {"node": "3", "field": "steps"}
        assert mapping["cfg"] == {"node": "3", "field": "cfg"}
        assert mapping["prompt"] == {"node": "6", "field": "text"}
        assert mapping["negative_prompt"] == {"node": "7", "field": "text"}
        assert mapping["width"] == {"node": "5", "field": "width"}
        assert mapping["height"] == {"node": "5", "field": "height"}
        assert mapping["checkpoint"] == {"node": "1", "field": "ckpt_name"}

    def test_maps_advanced_sampler_seed_and_lora(self) -> None:
        graph = make_graph()
        graph["3"]["class_type"] = "KSamplerAdvanced"
        del graph["3"]["inputs"]["seed"]
        graph["3"]["inputs"]["noise_seed"] = 1
        graph["8"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {"lora_name": "a.safetensors", "strength_model": 0.5},
        }
        mapping = infer_mapping(graph)
        assert mapping["seed"] == {"node": "3", "field": "noise_seed"}
        assert mapping["lora_name"] == {"node": "8", "field": "lora_name"}
        assert mapping["lora_strength_model"]["field"] == "strength_model"
        assert "lora_strength_clip" not in mapping

    def test_maps_load_image(self) -> None:
        graph = make_graph()
        graph["9"] = {"class_type": "LoadImage", "inputs": {"image": "in.png"}}
        assert infer_mapping(graph)["image"] == {"node": "9", "field": "image"}


class TestInjectInputs:
    def test_replaces_mapped_values_without_mutating_original(self) -> None:
        graph = make_graph()
        result = inject_inputs(graph, infer_mapping(graph), {"seed": 99, "prompt": "a dog"})
        assert result["3"]["inputs"]["seed"] == 99
        assert result["6"]["inputs"]["text"] == "a dog"
        assert graph["3"]["inputs"]["seed"] == 42
        assert graph["6"]["inputs"]["text"] == "a cat"

    def test_skips_none_and_unmapped_values(self) -> None:
        graph = make_graph()
        result = inject_inputs(graph, infer_mapping(graph), {"seed": None, "bogus": 1})
        assert result["3"]["inputs"]["seed"] == 42

    def test_missing_target_node_raises(self) -> None:
        with pytest.raises(WorkflowError, match="not in the workflow"):
            inject_inputs(make_graph(), {"seed": {"node": "99", "field": "seed"}}, {"seed": 1})

    def test_mapping_without_field_raises(self) -> None:
        with pytest.raises(WorkflowError, match="missing a 'field'"):
            inject_inputs(make_graph(), {"seed": {"node": "3"}}, {"seed": 1})


class TestFirstError:
    def test_extracts_error_message_truncated_to_500(self) -> None:
        status = {"messages": [["execution_error", "x" * 600]]}
        assert _first_error(status) == "x" * 500

    def test_ignores_non_error_messages_and_falls_back(self) -> None:
        status = {"messages": [["progress", "ok"]], "status_str": "error"}
        assert _first_error(status) == str(status)[:500]

    def test_empty_status_stringifies(self) -> None:
        assert _first_error({}) == "{}"


class TestWorkflowLibrary:
    def test_save_infers_mapping_and_writes_both_files(self, service) -> None:
        info = save_test_workflow(service)
        assert info.id == "test_wf"
        assert (service.workflow_dir / "test_wf.json").is_file()
        assert (service.workflow_dir / "test_wf.map.json").is_file()
        assert "seed" in info.inputs

    def test_save_sanitises_dots_in_the_name(self, service) -> None:
        info = service.save_workflow("Beta.WF", make_graph())
        assert info.id == "beta_wf"

    def test_save_rejects_unknown_mapped_inputs(self, service) -> None:
        with pytest.raises(WorkflowError, match="Unknown mapped inputs"):
            service.save_workflow("x", make_graph(), inputs={"bogus": {"node": "1", "field": "f"}})

    def test_save_rejects_invalid_graph(self, service) -> None:
        with pytest.raises(WorkflowError):
            service.save_workflow("x", {})

    def test_list_skips_map_files_and_broken_json(self, service) -> None:
        save_test_workflow(service)
        (service.workflow_dir / "broken.json").write_text("{not json", encoding="utf-8")
        ids = [w.id for w in service.list_workflows()]
        assert ids == ["test_wf"]

    def test_load_info_falls_back_to_title_name(self, service) -> None:
        (service.workflow_dir / "my_cool_wf.json").write_text(json.dumps(make_graph()), encoding="utf-8")
        infos = {w.id: w for w in service.list_workflows()}
        assert infos["my_cool_wf"].name == "My Cool Wf"
        assert infos["my_cool_wf"].node_count == len(make_graph())

    def test_load_missing_raises_404(self, service) -> None:
        with pytest.raises(WorkflowError) as exc:
            service.load_workflow("nope")
        assert exc.value.status_code == 404

    def test_load_invalid_json_raises(self, service) -> None:
        (service.workflow_dir / "bad.json").write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(WorkflowError, match="not valid JSON"):
            service.load_workflow("bad")

    def test_load_ui_format_graph_raises(self, service) -> None:
        (service.workflow_dir / "ui.json").write_text(json.dumps({"nodes": [1]}), encoding="utf-8")
        with pytest.raises(WorkflowError, match="UI-format"):
            service.load_workflow("ui")

    def test_load_returns_graph_and_mapping(self, service) -> None:
        save_test_workflow(service)
        graph, mapping = service.load_workflow("test_wf")
        assert set(graph) == {"1", "3", "5", "6", "7"}
        assert mapping["checkpoint"] == {"node": "1", "field": "ckpt_name"}

    def test_delete_removes_graph_and_map(self, service) -> None:
        save_test_workflow(service)
        assert service.delete_workflow("test_wf") is True
        assert not (service.workflow_dir / "test_wf.json").exists()
        assert not (service.workflow_dir / "test_wf.map.json").exists()
        assert service.delete_workflow("test_wf") is False


class TestBuildGraph:
    def test_keeps_an_explicit_seed(self, service) -> None:
        save_test_workflow(service)
        graph, seed = service.build_graph("test_wf", {"seed": 12345, "prompt": "a dog"})
        assert seed == 12345
        assert graph["3"]["inputs"]["seed"] == 12345
        assert graph["6"]["inputs"]["text"] == "a dog"

    def test_negative_seed_becomes_random(self, service) -> None:
        save_test_workflow(service)
        graph, seed = service.build_graph("test_wf", {"seed": -1})
        assert 0 <= seed < 2**32
        assert graph["3"]["inputs"]["seed"] == seed

    def test_missing_seed_becomes_random(self, service) -> None:
        save_test_workflow(service)
        _, seed = service.build_graph("test_wf", {})
        assert isinstance(seed, int) and seed >= 0

    def test_lora_neutralised_when_none_selected(self, service) -> None:
        save_lora_workflow(service)
        result, _ = service.build_graph("lora_wf", {"seed": 5})
        assert result["8"]["inputs"]["strength_model"] == 0.0
        assert result["8"]["inputs"]["strength_clip"] == 0.0

    def test_selected_lora_is_injected(self, service) -> None:
        save_lora_workflow(service)
        result, _ = service.build_graph(
            "lora_wf", {"seed": 5, "lora_name": "style.safetensors", "lora_strength_model": 0.4}
        )
        assert result["8"]["inputs"]["lora_name"] == "style.safetensors"
        assert result["8"]["inputs"]["strength_model"] == 0.4


class TestRunGenerationSync:
    def test_end_to_end_with_fake_client(self, service, fake_client) -> None:
        save_test_workflow(service)
        record = {
            "status": {"completed": True},
            "outputs": {"9": {"images": [{"filename": "out.png", "subfolder": "", "type": "output"}]}},
        }
        fake_client.history_records["pid-1"] = [{}, record]
        fake_client.view_results["out.png"] = b"PNGDATA"
        calls: list[tuple[float, str]] = []

        def progress(value: float, message: str) -> None:
            calls.append((value, message))

        saved = service.run_generation_sync("test_wf", {"prompt": "a cat", "seed": 7}, progress=progress)

        assert saved == ["pid-1_0.png"]
        assert (service.output_dir / "pid-1_0.png").read_bytes() == b"PNGDATA"
        queued = fake_client.queued_graphs[0]
        assert queued["3"]["inputs"]["seed"] == 7
        assert queued["6"]["inputs"]["text"] == "a cat"
        assert calls[0][0] == 0.05
        assert calls[-1] == (1.0, "Generated 1 image(s)")

    def test_cancelled_returns_empty_without_polling(self, service, fake_client) -> None:
        save_test_workflow(service)
        saved = service.run_generation_sync("test_wf", {}, cancelled=lambda: True)
        assert saved == []
        assert fake_client.history_calls == 0

    @pytest.mark.parametrize(
        "record",
        [
            {"status": {"status_str": "error", "messages": [["execution_error", "boom"]]}},
            {"status": {"completed": False, "messages": []}},
        ],
        ids=["status_str_error", "completed_false"],
    )
    def test_execution_error_raises(self, service, fake_client, record: dict[str, Any]) -> None:
        save_test_workflow(service)
        fake_client.history_records["pid-1"] = [record]
        with pytest.raises(ComfyUIError) as exc:
            service.run_generation_sync("test_wf", {})
        assert "execution error" in str(exc.value).lower()

    def test_timeout_raises_504(self, service, fake_client, monkeypatch: pytest.MonkeyPatch) -> None:
        save_test_workflow(service)
        monkeypatch.setattr(settings, "comfyui_generation_timeout", 0.2)
        monkeypatch.setattr(settings, "comfyui_poll_interval", 0.05)
        with pytest.raises(ComfyUIError) as exc:
            service.run_generation_sync("test_wf", {})
        assert exc.value.status_code == 504
        assert "timed out" in str(exc.value)


class TestSaveOutputsAndFiles:
    def test_saves_each_image_and_skips_bad_ones(self, service, fake_client) -> None:
        fake_client.view_errors.add("bad.png")
        saved = service._save_outputs(
            "job",
            {
                "9": {
                    "images": [
                        {"filename": "bad.png"},
                        {"filename": None},
                        {"filename": "good.jpg", "subfolder": "runs", "type": "input"},
                    ]
                }
            },
        )
        assert saved == ["job_0.jpg"]
        assert (service.output_dir / "job_0.jpg").read_bytes() == b"default-bytes"
        assert fake_client.view_calls[-1] == ("good.jpg", "runs", "input")

    def test_no_images_gives_empty_list(self, service) -> None:
        assert service._save_outputs("job", {"9": {}}) == []

    def test_read_generated_returns_bytes(self, service) -> None:
        (service.output_dir / "img.png").write_bytes(b"data")
        assert service.read_generated("img.png") == b"data"

    def test_read_generated_missing_raises_404(self, service) -> None:
        with pytest.raises(WorkflowError) as exc:
            service.read_generated("missing.png")
        assert exc.value.status_code == 404

    def test_write_provenance_creates_sidecars(self, service) -> None:
        (service.output_dir / "job_0.png").write_bytes(b"x")
        service.write_provenance(["job_0.png"], {"workflow_id": "gen", "seed": 7})
        record = json.loads((service.output_dir / "job_0.provenance.json").read_text(encoding="utf-8"))
        assert record["image"] == "job_0.png"
        assert record["seed"] == 7
        assert "created_at" in record

    def test_list_generated_orders_by_mtime_and_includes_provenance(self, service) -> None:
        a = service.output_dir / "a.png"
        b = service.output_dir / "b.jpg"
        a.write_bytes(b"AAA")
        b.write_bytes(b"BBB")
        (service.output_dir / "notes.txt").write_text("hi", encoding="utf-8")
        os.utime(a, (1000, 1000))
        os.utime(b, (2000, 2000))
        service.write_provenance(["a.png"], {"workflow_id": "gen"})

        entries = service.list_generated()
        assert [e["filename"] for e in entries] == ["b.jpg", "a.png"]
        assert entries[1]["provenance"]["workflow_id"] == "gen"
        assert entries[0]["size_bytes"] == 3

    def test_list_generated_respects_limit(self, service) -> None:
        a = service.output_dir / "a.png"
        b = service.output_dir / "b.jpg"
        a.write_bytes(b"A")
        b.write_bytes(b"B")
        os.utime(a, (1000, 1000))
        os.utime(b, (2000, 2000))
        entries = service.list_generated(limit=1)
        assert [e["filename"] for e in entries] == ["b.jpg"]


class TestValidateWorkflowRequest:
    def test_missing_workflow_fails(self, service) -> None:
        result = service.validate_workflow_request("nope", {})
        assert result["valid"] is False
        assert result["checks"][0] == {"name": "Workflow", "passed": False, "detail": "Workflow 'nope' not found"}
        assert result["errors"]

    def test_unsupported_params_fail(self, service) -> None:
        save_test_workflow(service)
        result = service.validate_workflow_request("test_wf", {"bogus": 1})
        inputs_check = next(c for c in result["checks"] if c["name"] == "Inputs")
        assert inputs_check["passed"] is False
        assert "bogus" in inputs_check["detail"]
        assert result["valid"] is False

    def test_supported_params_pass_when_connected(self, service, fake_client) -> None:
        save_lora_workflow(service)
        fake_client.ping_result = {"system": {}}
        result = service.validate_workflow_request(
            "lora_wf", {"checkpoint": "v1-5-pruned.ckpt", "lora_name": "style.safetensors"}
        )
        assert result["valid"] is True
        assert result["errors"] == []
        by_name = {c["name"]: c for c in result["checks"]}
        assert by_name["Inputs"]["passed"] is True
        assert by_name["ComfyUI connection"]["passed"] is True
        assert by_name["Checkpoint"]["passed"] is True
        assert by_name["LoRA"]["passed"] is True

    def test_unknown_checkpoint_fails(self, service, fake_client) -> None:
        save_lora_workflow(service)
        fake_client.ping_result = {"system": {}}
        result = service.validate_workflow_request("lora_wf", {"checkpoint": "other.ckpt"})
        assert result["valid"] is False
        detail = next(c for c in result["checks"] if c["name"] == "Checkpoint")["detail"]
        assert "not installed" in detail

    def test_unknown_lora_fails(self, service, fake_client) -> None:
        save_lora_workflow(service)
        fake_client.ping_result = {"system": {}}
        result = service.validate_workflow_request("lora_wf", {"lora_name": "missing.safetensors"})
        assert result["valid"] is False
        detail = next(c for c in result["checks"] if c["name"] == "LoRA")["detail"]
        assert "not in ComfyUI's loras folder" in detail

    def test_not_connected_fails(self, service, fake_client) -> None:
        save_test_workflow(service)
        fake_client.ping_result = ComfyUIError("ComfyUI not reachable at http://comfy.test (503)", detail="refused")
        result = service.validate_workflow_request("test_wf", {})
        assert result["valid"] is False
        conn = next(c for c in result["checks"] if c["name"] == "ComfyUI connection")
        assert conn["passed"] is False
        assert "not reachable" in conn["detail"]

    def test_empty_params_report_defaults(self, service, fake_client) -> None:
        save_test_workflow(service)
        fake_client.ping_result = {"system": {}}
        result = service.validate_workflow_request("test_wf", {})
        inputs_check = next(c for c in result["checks"] if c["name"] == "Inputs")
        assert inputs_check["passed"] is True
        assert inputs_check["detail"] == "defaults"


class TestPrepareLoraTest:
    def test_no_lora_workflow_raises_404(self, service) -> None:
        save_test_workflow(service)
        with pytest.raises(WorkflowError) as exc:
            service.prepare_lora_test("style.safetensors")
        assert exc.value.status_code == 404

    def test_unknown_workflow_id_raises_404(self, service) -> None:
        save_lora_workflow(service)
        with pytest.raises(WorkflowError) as exc:
            service.prepare_lora_test("x.safetensors", workflow_id="nope")
        assert exc.value.status_code == 404

    def test_workflow_without_lora_node_raises_400(self, service) -> None:
        save_test_workflow(service)
        with pytest.raises(WorkflowError) as exc:
            service.prepare_lora_test("x.safetensors", workflow_id="test_wf")
        assert exc.value.status_code == 400

    def test_not_connected_adds_warning_and_builds_request(self, service, fake_client) -> None:
        save_lora_workflow(service)
        fake_client.ping_result = ComfyUIError("ComfyUI not reachable at http://comfy.test (503)", detail="refused")
        result = service.prepare_lora_test("style.safetensors", trigger_word="kitty")
        assert result["connected"] is False
        assert any("not reachable" in w for w in result["warnings"])
        request = result["generation_request"]
        assert request["workflow_id"] == "lora_wf"
        assert request["prompt"] == "kitty, portrait photo, soft lighting"
        assert request["lora_name"] == "style.safetensors"

    def test_connected_and_known_lora_has_no_warnings(self, service, fake_client) -> None:
        save_lora_workflow(service)
        fake_client.ping_result = {"system": {}}
        result = service.prepare_lora_test("style.safetensors", prompt="a cat")
        assert result["connected"] is True
        assert result["warnings"] == []
        assert result["generation_request"]["prompt"] == "a cat"

    def test_default_prompt_without_trigger_word(self, service, fake_client) -> None:
        save_lora_workflow(service)
        fake_client.ping_result = {"system": {}}
        result = service.prepare_lora_test("style.safetensors")
        assert result["generation_request"]["prompt"] == "portrait photo, soft lighting"


class TestStatus:
    def test_connected_reports_version_devices_and_models(self, service, fake_client) -> None:
        fake_client.ping_result = {
            "system": {"comfyui_version": "0.3.45", "python_version": "3.12"},
            "devices": [{"name": "cuda:0", "vram_total": 8589934592, "vram_free": 4294967296}],
        }
        info = service.status()
        assert info["connected"] is True
        assert info["version"] == "0.3.45"
        assert info["devices"][0]["name"] == "cuda:0"
        assert info["checkpoints"] == ["v1-5-pruned.ckpt"]
        assert info["loras"] == ["style.safetensors"]

    def test_ping_failure_reports_error_detail(self, service, fake_client) -> None:
        err = ComfyUIError(
            "ComfyUI not reachable at http://comfy.test (503)", detail="connection refused"
        )
        fake_client.ping_result = err
        info = service.status()
        assert info["connected"] is False
        assert info["error"] == "connection refused"

    def test_model_listing_failure_yields_empty_lists(self, service, fake_client) -> None:
        fake_client.ping_result = {}
        fake_client.models_error = ComfyUIError("boom", detail="nope")
        info = service.status()
        assert info["connected"] is True
        assert info["checkpoints"] == []
        assert info["loras"] == []
