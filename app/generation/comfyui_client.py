"""HTTP client for a locally running ComfyUI instance.

Ported from Apollo `app/clients/comfyui_client.py` (proven transport logic,
unchanged behavior) into the SPARTON generation domain.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)


class ComfyUIError(Exception):
    def __init__(self, message: str, status_code: int = 502, detail: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail or message


class ComfyUIClient:
    """Talks to ComfyUI's HTTP API (``/prompt``, ``/history``, ``/view``, ...)."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self.base_url = (base_url or settings.comfyui_base_url).rstrip("/")
        self.timeout = timeout or settings.comfyui_timeout
        self.client_id = uuid.uuid4().hex

    # ---------- transport ----------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
        data: dict | None = None,
        raw: bool = False,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = requests.request(
                method, url, json=json, params=params, files=files, data=data,
                timeout=timeout or self.timeout,
            )
        except requests.Timeout as exc:
            raise ComfyUIError(
                f"ComfyUI timed out after {timeout or self.timeout:.0f}s",
                status_code=504, detail=str(exc),
            ) from exc
        except requests.RequestException as exc:
            raise ComfyUIError(
                f"ComfyUI is not reachable at {self.base_url}",
                status_code=503, detail=str(exc),
            ) from exc

        if response.status_code >= 400:
            raise ComfyUIError(
                f"ComfyUI rejected the request ({response.status_code})",
                status_code=502, detail=_extract_error(response),
            )

        if raw:
            return response.content
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ComfyUIError("ComfyUI returned a malformed response", detail=str(exc)) from exc

    # ---------- api ----------

    def ping(self) -> dict:
        stats = self._request("GET", "/system_stats", timeout=min(self.timeout, 5))
        if not isinstance(stats, dict):
            raise ComfyUIError("ComfyUI returned unexpected system stats")
        return stats

    def object_info(self, node_class: str | None = None) -> dict:
        path = f"/object_info/{node_class}" if node_class else "/object_info"
        result = self._request("GET", path)
        return result if isinstance(result, dict) else {}

    def queue_prompt(self, workflow: dict) -> str:
        payload = self._request(
            "POST", "/prompt", json={"prompt": workflow, "client_id": self.client_id}
        )
        prompt_id = payload.get("prompt_id") if isinstance(payload, dict) else None
        if not prompt_id:
            node_errors = (payload or {}).get("node_errors") or {}
            raise ComfyUIError(
                "ComfyUI did not accept the workflow",
                detail=str(node_errors) or str(payload),
            )
        logger.info("[COMFYUI] Queued prompt %s", prompt_id)
        return str(prompt_id)

    def history(self, prompt_id: str) -> dict:
        result = self._request("GET", f"/history/{prompt_id}")
        if isinstance(result, dict):
            return result.get(prompt_id, {}) or {}
        return {}

    def queue_state(self) -> dict:
        result = self._request("GET", "/queue")
        return result if isinstance(result, dict) else {}

    def view_image(self, filename: str, subfolder: str = "", folder_type: str = "output") -> bytes:
        return self._request(
            "GET",
            "/view",
            params={"filename": filename, "subfolder": subfolder, "type": folder_type},
            raw=True,
        )

    def upload_image(self, filename: str, content: bytes, overwrite: bool = True) -> dict:
        result = self._request(
            "POST",
            "/upload/image",
            files={"image": (filename, content)},
            data={"overwrite": str(overwrite).lower()},
        )
        return result if isinstance(result, dict) else {}

    # ---------- model discovery ----------

    def _combo_options(self, node_class: str, input_name: str) -> list[str]:
        info = self.object_info(node_class).get(node_class, {})
        required = info.get("input", {}).get("required", {})
        spec = required.get(input_name)
        if isinstance(spec, list) and spec and isinstance(spec[0], list):
            return [str(option) for option in spec[0]]
        return []

    def list_loras(self) -> list[str]:
        return self._combo_options("LoraLoader", "lora_name")

    def list_checkpoints(self) -> list[str]:
        return self._combo_options("CheckpointLoaderSimple", "ckpt_name")


def _extract_error(response: requests.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:500]
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
        if body.get("node_errors"):
            return str(body["node_errors"])[:500]
    return str(body)[:500]
