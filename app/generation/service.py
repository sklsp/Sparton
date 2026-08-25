"""ComfyUI orchestration: workflow library, input injection, generation.

Ported from Apollo `app/services/comfyui_service.py`. The workflow library,
graph validation, auto-mapping, and injection logic are kept as-is (proven);
generation now runs on SPARTON's durable Job system instead of Apollo's
process-local thread store, and completed images are recorded as
tenant-scoped ``GeneratedImage`` rows with provenance.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.generation.comfyui_client import ComfyUIError, ComfyUIClient

logger = logging.getLogger(__name__)

KNOWN_INPUTS = (
    "prompt", "negative_prompt", "seed", "steps", "cfg",
    "width", "height", "checkpoint", "image",
    "lora_name", "lora_strength_model", "lora_strength_clip",
)

_SAMPLER_CLASSES = {"KSampler", "KSamplerAdvanced"}
_LATENT_CLASSES = {"EmptyLatentImage", "EmptySD3LatentImage", "EmptyLatentImagePresets"}
_LORA_CLASSES = {"LoraLoader", "LoraLoaderModelOnly"}


class WorkflowError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class WorkflowInfo:
    id: str
    name: str
    description: str = ""
    arch: str = "generic"
    inputs: dict[str, dict[str, str]] = field(default_factory=dict)
    node_count: int = 0

    @property
    def supported_inputs(self) -> list[str]:
        return sorted(self.inputs.keys())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "arch": self.arch,
            "inputs": self.inputs,
            "supported_inputs": self.supported_inputs,
            "node_count": self.node_count,
        }


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json_atomic(path: Path, data: Any) -> None:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def validate_graph(graph: Any) -> dict:
    """Check that ``graph`` is a ComfyUI API-format workflow."""
    if not isinstance(graph, dict) or not graph:
        raise WorkflowError("Workflow must be a non-empty JSON object")

    if "nodes" in graph and isinstance(graph.get("nodes"), list):
        raise WorkflowError(
            "This looks like a ComfyUI UI-format workflow. Re-export it with "
            "'Workflow > Export (API)' and import that file instead."
        )

    for node_id, node in graph.items():
        if not isinstance(node, dict):
            raise WorkflowError(f"Node '{node_id}' is not an object")
        if not isinstance(node.get("class_type"), str):
            raise WorkflowError(f"Node '{node_id}' is missing a string 'class_type'")
        if not isinstance(node.get("inputs"), dict):
            raise WorkflowError(f"Node '{node_id}' is missing an 'inputs' object")
    return graph


def infer_mapping(graph: dict) -> dict[str, dict[str, str]]:
    """Best-effort auto-mapping so an imported workflow is usable immediately."""
    mapping: dict[str, dict[str, str]] = {}

    def put(name: str, node_id: str, field_name: str) -> None:
        mapping.setdefault(name, {"node": str(node_id), "field": field_name})

    for node_id, node in graph.items():
        class_type = node.get("class_type", "")
        inputs = node.get("inputs", {})

        if class_type in _SAMPLER_CLASSES:
            for logical, field_name in (
                ("seed", "seed"),
                ("seed", "noise_seed"),
                ("steps", "steps"),
                ("cfg", "cfg"),
            ):
                if field_name in inputs:
                    put(logical, node_id, field_name)
            for logical, link_name in (("prompt", "positive"), ("negative_prompt", "negative")):
                link = inputs.get(link_name)
                if isinstance(link, list) and link:
                    target = graph.get(str(link[0]), {})
                    if "text" in target.get("inputs", {}):
                        put(logical, str(link[0]), "text")

        elif class_type in _LATENT_CLASSES:
            if "width" in inputs:
                put("width", node_id, "width")
            if "height" in inputs:
                put("height", node_id, "height")

        elif class_type in _LORA_CLASSES:
            put("lora_name", node_id, "lora_name")
            if "strength_model" in inputs:
                put("lora_strength_model", node_id, "strength_model")
            if "strength_clip" in inputs:
                put("lora_strength_clip", node_id, "strength_clip")

        elif class_type == "CheckpointLoaderSimple":
            put("checkpoint", node_id, "ckpt_name")

        elif class_type == "LoadImage" and "image" in inputs:
            put("image", node_id, "image")

    return mapping


def inject_inputs(graph: dict, mapping: dict, params: dict[str, Any]) -> dict:
    """Return a copy of ``graph`` with mapped inputs replaced by ``params``."""
    result = copy.deepcopy(graph)

    for name, value in params.items():
        if value is None or name not in mapping:
            continue
        target = mapping[name]
        node_id, field_name = str(target.get("node")), target.get("field")
        node = result.get(node_id)
        if node is None:
            raise WorkflowError(
                f"Workflow mapping points at node '{node_id}' for '{name}', "
                "but that node is not in the workflow"
            )
        if not field_name:
            raise WorkflowError(f"Mapping for '{name}' is missing a 'field'")
        node.setdefault("inputs", {})[field_name] = value

    return result


class ComfyUIService:
    """Workflow library plus generation orchestration."""

    def __init__(
        self,
        client: ComfyUIClient | None = None,
        workflow_dir: str | None = None,
        output_dir: str | None = None,
    ) -> None:
        self.client = client or ComfyUIClient()
        self.workflow_dir = Path(workflow_dir or settings.comfyui_workflow_dir)
        self.output_dir = Path(output_dir or settings.generated_dir)
        self.workflow_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ---------- status ----------

    def status(self) -> dict[str, Any]:
        info: dict[str, Any] = {"base_url": self.client.base_url, "connected": False}
        try:
            stats = self.client.ping()
        except ComfyUIError as exc:
            info["error"] = exc.detail
            return info

        system = stats.get("system", {})
        info.update(
            connected=True,
            version=system.get("comfyui_version"),
            python_version=system.get("python_version"),
            devices=[
                {
                    "name": device.get("name"),
                    "vram_total": device.get("vram_total"),
                    "vram_free": device.get("vram_free"),
                }
                for device in stats.get("devices", [])
            ],
        )
        try:
            info["checkpoints"] = self.client.list_checkpoints()
            info["loras"] = self.client.list_loras()
        except ComfyUIError as exc:
            logger.warning("[COMFYUI] Could not list models: %s", exc.detail)
            info["checkpoints"] = []
            info["loras"] = []
        return info

    # ---------- workflow library ----------

    def _graph_path(self, workflow_id: str) -> Path:
        from app.core.security.paths import safe_join

        return safe_join(self.workflow_dir, f"{workflow_id}.json")

    def _map_path(self, workflow_id: str) -> Path:
        from app.core.security.paths import safe_join

        return safe_join(self.workflow_dir, f"{workflow_id}.map.json")

    def list_workflows(self) -> list[WorkflowInfo]:
        workflows: list[WorkflowInfo] = []
        for path in sorted(self.workflow_dir.glob("*.json")):
            if path.name.endswith(".map.json"):
                continue
            try:
                workflows.append(self._load_info(path.stem))
            except WorkflowError as exc:
                logger.warning("[COMFYUI] Skipping workflow %s: %s", path.stem, exc.message)
        return workflows

    def _load_info(self, workflow_id: str) -> WorkflowInfo:
        graph = _read_json(self._graph_path(workflow_id))
        if not isinstance(graph, dict):
            raise WorkflowError(f"Workflow '{workflow_id}' could not be read")
        meta = _read_json(self._map_path(workflow_id), default={}) or {}
        return WorkflowInfo(
            id=workflow_id,
            name=meta.get("name", workflow_id.replace("_", " ").title()),
            description=meta.get("description", ""),
            arch=meta.get("arch", "generic"),
            inputs=meta.get("inputs", {}),
            node_count=len(graph),
        )

    def load_workflow(self, workflow_id: str) -> tuple[dict, dict]:
        graph_path = self._graph_path(workflow_id)
        if not graph_path.is_file():
            raise WorkflowError(f"Workflow '{workflow_id}' not found", status_code=404)

        graph = _read_json(graph_path)
        if not isinstance(graph, dict):
            raise WorkflowError(f"Workflow '{workflow_id}' is not valid JSON")
        validate_graph(graph)

        meta = _read_json(self._map_path(workflow_id), default={}) or {}
        return graph, meta.get("inputs", {})

    def save_workflow(
        self,
        name: str,
        graph: dict,
        description: str = "",
        arch: str = "generic",
        inputs: dict | None = None,
    ) -> WorkflowInfo:
        from app.core.security.paths import sanitize_filename

        validate_graph(graph)

        workflow_id = sanitize_filename(name, default="workflow").removesuffix(".json")
        workflow_id = workflow_id.replace(".", "_").lower() or "workflow"

        mapping = inputs if inputs else infer_mapping(graph)
        unknown = set(mapping) - set(KNOWN_INPUTS)
        if unknown:
            raise WorkflowError(f"Unknown mapped inputs: {', '.join(sorted(unknown))}")

        _write_json_atomic(self._graph_path(workflow_id), graph)
        _write_json_atomic(
            self._map_path(workflow_id),
            {"name": name, "description": description, "arch": arch, "inputs": mapping},
        )
        logger.info("[COMFYUI] Saved workflow '%s' (%d nodes)", workflow_id, len(graph))
        return self._load_info(workflow_id)

    def delete_workflow(self, workflow_id: str) -> bool:
        graph_path = self._graph_path(workflow_id)
        if not graph_path.is_file():
            return False
        graph_path.unlink()
        self._map_path(workflow_id).unlink(missing_ok=True)
        return True

    # ---------- generation ----------

    def build_graph(self, workflow_id: str, params: dict[str, Any]) -> tuple[dict, int]:
        """Prepare a submittable graph. Returns ``(graph, resolved_seed)``."""
        graph, mapping = self.load_workflow(workflow_id)

        params = dict(params)
        seed = params.get("seed")
        if seed is None or int(seed) < 0:
            seed = random.randint(0, 2**32 - 1)
        params["seed"] = int(seed)

        # A workflow with a LoRA node but no LoRA selected is neutralised by
        # zeroing its strengths rather than rewiring the graph.
        if "lora_name" in mapping and not params.get("lora_name"):
            params.pop("lora_name", None)
            params["lora_strength_model"] = 0.0
            params["lora_strength_clip"] = 0.0

        return inject_inputs(graph, mapping, params), int(seed)

    def run_generation_sync(
        self,
        workflow_id: str,
        params: dict[str, Any],
        *,
        progress=None,
        cancelled=None,
    ) -> list[str]:
        """Run one generation to completion; returns saved local filenames.

        ``progress(fraction, message)`` and ``cancelled() -> bool`` hooks let
        the shared job runner report status and honor cancellation.
        """
        graph, seed = self.build_graph(workflow_id, params)
        if progress:
            progress(0.05, "Submitting to ComfyUI")

        prompt_id = self.client.queue_prompt(graph)

        deadline = time.monotonic() + settings.comfyui_generation_timeout
        while time.monotonic() < deadline:
            if cancelled and cancelled():
                logger.info("[COMFYUI] Generation cancelled by user")
                return []

            record = self.client.history(prompt_id)
            if record:
                status = record.get("status", {})
                if status.get("status_str") == "error" or status.get("completed") is False:
                    raise ComfyUIError(
                        "ComfyUI reported an execution error",
                        detail=_first_error(status),
                    )
                if record.get("outputs"):
                    saved = self._save_outputs(str(prompt_id), record["outputs"])
                    if progress:
                        progress(1.0, f"Generated {len(saved)} image(s)")
                    return saved
            elif progress:
                progress(0.3, "Waiting for ComfyUI")

            time.sleep(settings.comfyui_poll_interval)

        raise ComfyUIError(
            f"Generation timed out after {settings.comfyui_generation_timeout:.0f}s",
            status_code=504,
        )

    def _save_outputs(self, job_key: str, outputs: dict) -> list[str]:
        saved: list[str] = []
        for node_output in outputs.values():
            for index, image in enumerate(node_output.get("images", [])):
                filename = image.get("filename")
                if not filename:
                    continue
                try:
                    content = self.client.view_image(
                        filename,
                        image.get("subfolder", ""),
                        image.get("type", "output"),
                    )
                except ComfyUIError as exc:
                    logger.warning("[COMFYUI] Could not fetch %s: %s", filename, exc.detail)
                    continue

                suffix = Path(filename).suffix or ".png"
                local_name = f"{job_key}_{len(saved)}{suffix}"
                (self.output_dir / local_name).write_bytes(content)
                saved.append(local_name)
        return saved

    def read_generated(self, filename: str) -> bytes:
        from app.core.security.paths import safe_join, sanitize_filename

        path = safe_join(self.output_dir, sanitize_filename(filename))
        if not path.is_file():
            raise WorkflowError(f"Image '{filename}' not found", status_code=404)
        return path.read_bytes()

    def write_provenance(self, filenames: list[str], record: dict[str, Any]) -> None:
        base = {
            **record,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        }
        for filename in filenames:
            sidecar = self.output_dir / f"{Path(filename).stem}.provenance.json"
            try:
                _write_json_atomic(sidecar, {**base, "image": filename})
            except OSError as exc:
                logger.warning("[COMFYUI] Could not write provenance for %s: %s", filename, exc)

    def list_generated(self, limit: int = 50) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        images = sorted(
            (p for p in self.output_dir.iterdir()
             if p.suffix.lower() in (".png", ".jpg", ".jpeg") and p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in images[:limit]:
            entry: dict[str, Any] = {
                "filename": path.name,
                "size_bytes": path.stat().st_size,
                "created_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.gmtime(path.stat().st_mtime)
                ),
            }
            sidecar = self.output_dir / f"{path.stem}.provenance.json"
            if sidecar.is_file():
                record = _read_json(sidecar, default=None)
                if isinstance(record, dict):
                    entry["provenance"] = record
            entries.append(entry)
        return entries

    # ---------- preflight ----------

    def validate_workflow_request(
        self,
        workflow_id: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Pre-flight a generation request against the live ComfyUI."""
        checks: list[dict[str, Any]] = []
        ok = True

        try:
            graph, mapping = self.load_workflow(workflow_id)
            checks.append({"name": "Workflow", "passed": True, "detail": f"{len(graph)} nodes"})
        except WorkflowError as exc:
            return {
                "valid": False,
                "checks": [{"name": "Workflow", "passed": False, "detail": exc.message}],
                "errors": [exc.message],
            }

        unsupported = [n for n in params if n not in mapping and params[n] is not None]
        if unsupported:
            ok = False
            checks.append({
                "name": "Inputs", "passed": False,
                "detail": f"workflow does not map: {', '.join(sorted(unsupported))}",
            })
        else:
            used = [n for n in params if n in mapping and params[n] is not None]
            checks.append({
                "name": "Inputs", "passed": True,
                "detail": ", ".join(sorted(used)) or "defaults",
            })

        connected = False
        checkpoints: list[str] = []
        loras: list[str] = []
        try:
            status = self.status()
            connected = bool(status.get("connected"))
            checkpoints = status.get("checkpoints") or []
            loras = status.get("loras") or []
        except ComfyUIError:
            pass

        if not connected:
            checks.append({
                "name": "ComfyUI connection", "passed": False,
                "detail": f"not reachable at {self.client.base_url} — start ComfyUI, then retry",
            })
            ok = False
        else:
            checks.append({"name": "ComfyUI connection", "passed": True, "detail": self.client.base_url})

            checkpoint = params.get("checkpoint")
            if checkpoint and checkpoints and checkpoint not in checkpoints:
                ok = False
                checks.append({
                    "name": "Checkpoint", "passed": False,
                    "detail": f"'{checkpoint}' not installed; available: {', '.join(checkpoints[:5])}",
                })
            elif checkpoint:
                checks.append({"name": "Checkpoint", "passed": True, "detail": checkpoint})

            lora_name = params.get("lora_name")
            if lora_name and loras and lora_name not in loras:
                ok = False
                checks.append({
                    "name": "LoRA", "passed": False,
                    "detail": f"'{lora_name}' is not in ComfyUI's loras folder",
                })
            elif lora_name:
                checks.append({"name": "LoRA", "passed": True, "detail": lora_name})

        return {
            "valid": ok,
            "checks": checks,
            "errors": [] if ok else [c["detail"] for c in checks if not c["passed"]],
        }

    def prepare_lora_test(
        self,
        lora_filename: str,
        workflow_id: str | None = None,
        prompt: str | None = None,
        trigger_word: str | None = None,
    ) -> dict[str, Any]:
        workflows = self.list_workflows()
        candidates = [w for w in workflows if "lora_name" in w.supported_inputs]

        chosen: WorkflowInfo | None = None
        if workflow_id:
            requested = next((w for w in workflows if w.id == workflow_id), None)
            if requested is None:
                raise WorkflowError(f"Workflow '{workflow_id}' does not exist", status_code=404)
            if "lora_name" not in requested.supported_inputs:
                raise WorkflowError(
                    f"Workflow '{workflow_id}' has no LoRA node, so it cannot be used to test a LoRA",
                    status_code=400,
                )
            chosen = requested
        elif not candidates:
            raise WorkflowError(
                "No saved workflow has a LoRA node. Import one with a 'LoraLoader' node to test LoRAs.",
                status_code=404,
            )
        else:
            chosen = candidates[0]

        connected = False
        visible_loras: list[str] = []
        try:
            status = self.status()
            connected = bool(status.get("connected"))
            visible_loras = status.get("loras") or []
        except ComfyUIError:
            pass

        warnings: list[str] = []
        if not connected:
            warnings.append("ComfyUI is not reachable; start it before generating.")
        elif lora_filename not in visible_loras:
            warnings.append(
                f"'{lora_filename}' is not in ComfyUI's loras folder yet. Copy "
                "it there (or set COMFYUI_LORA_DIR) and restart ComfyUI."
            )

        full_prompt = prompt or (
            f"{trigger_word}, portrait photo, soft lighting" if trigger_word
            else "portrait photo, soft lighting"
        )

        return {
            "workflow": chosen.to_dict(),
            "generation_request": {
                "workflow_id": chosen.id,
                "prompt": full_prompt,
                "lora_name": lora_filename,
                "lora_strength_model": 0.8,
                "lora_strength_clip": 0.8,
            },
            "connected": connected,
            "warnings": warnings,
        }


def _first_error(status: dict) -> str:
    for message in status.get("messages", []):
        if isinstance(message, list) and len(message) > 1 and "error" in str(message[0]):
            return str(message[1])[:500]
    return str(status)[:500]
