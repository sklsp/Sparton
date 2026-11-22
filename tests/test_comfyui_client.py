"""ComfyUI client tests. Every HTTP call is faked at requests.request; no server, no network."""

from __future__ import annotations

import json
from typing import cast

import pytest
import requests

from app.core.config import settings
from app.generation.comfyui_client import ComfyUIClient, ComfyUIError


class FakeResponse:
    """Minimal stand-in for requests.Response, driven by raw content like the real one."""

    def __init__(self, status_code=200, payload=None, content=b"", text=""):
        self.status_code = status_code
        if payload is not None:
            self._content = json.dumps(payload).encode("utf-8")
        else:
            self._content = content
        self.text = text or self._content.decode("utf-8", "replace")

    @property
    def content(self):
        return self._content

    def json(self):
        try:
            return json.loads(self._content)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


def patch_request(monkeypatch, *responses):
    """Make requests.request return `responses` in order; record the calls."""
    calls = []
    queue = list(responses)

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        item = queue.pop(0) if queue else responses[-1]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests, "request", fake_request)
    return calls


@pytest.fixture()
def client():
    return ComfyUIClient(base_url="http://comfy.test", timeout=30.0)


# ---------------------------------------------------------------------------
# Construction and settings defaults
# ---------------------------------------------------------------------------
class TestClientInit:
    def test_defaults_come_from_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "comfyui_base_url", "http://from-settings/")
        monkeypatch.setattr(settings, "comfyui_timeout", 7.5)
        client = ComfyUIClient()
        assert client.base_url == "http://from-settings"
        assert client.timeout == 7.5

    def test_explicit_arguments_win(self):
        client = ComfyUIClient(base_url="http://explicit/", timeout=1.0)
        assert client.base_url == "http://explicit"
        assert client.timeout == 1.0

    def test_client_id_is_a_unique_hex_string(self):
        a = ComfyUIClient(base_url="http://a", timeout=1.0)
        b = ComfyUIClient(base_url="http://b", timeout=1.0)
        assert len(a.client_id) == 32
        assert all(c in "0123456789abcdef" for c in a.client_id)
        assert a.client_id != b.client_id


# ---------------------------------------------------------------------------
# Status: up, down, an error answer
# ---------------------------------------------------------------------------
class TestStatus:
    def test_up_returns_the_system_stats(self, client, monkeypatch):
        stats = {"system": {"os": "linux"}, "devices": [{"name": "cuda"}]}
        calls = patch_request(monkeypatch, FakeResponse(200, stats))
        assert client.ping() == stats
        assert calls[0]["method"] == "GET"
        assert str(calls[0]["url"]) == "http://comfy.test/system_stats"

    def test_ping_caps_its_timeout_at_five_seconds(self, monkeypatch):
        slow = ComfyUIClient(base_url="http://comfy.test", timeout=30.0)
        calls = patch_request(monkeypatch, FakeResponse(200, {}))
        slow.ping()
        assert calls[0]["timeout"] == 5

    def test_ping_keeps_a_shorter_client_timeout(self, monkeypatch):
        fast = ComfyUIClient(base_url="http://comfy.test", timeout=2.0)
        calls = patch_request(monkeypatch, FakeResponse(200, {}))
        fast.ping()
        assert calls[0]["timeout"] == 2

    def test_down_becomes_an_unreachable_error(self, client, monkeypatch):
        patch_request(monkeypatch, requests.ConnectionError("connection refused"))
        with pytest.raises(ComfyUIError) as exc:
            client.ping()
        assert exc.value.status_code == 503
        assert "not reachable" in str(exc.value)
        assert "http://comfy.test" in str(exc.value)

    def test_http_error_answer_becomes_a_502(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(404, {"error": {"message": "no such endpoint"}}))
        with pytest.raises(ComfyUIError) as exc:
            client.ping()
        assert exc.value.status_code == 502
        assert exc.value.detail == "no such endpoint"

    def test_non_dict_stats_are_rejected(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, ["not", "a", "dict"]))
        with pytest.raises(ComfyUIError) as exc:
            client.ping()
        assert "unexpected system stats" in str(exc.value)


# ---------------------------------------------------------------------------
# Queueing a prompt
# ---------------------------------------------------------------------------
class TestQueuePrompt:
    def test_returns_the_prompt_id_and_sends_the_workflow(self, client, monkeypatch):
        workflow = {"3": {"class_type": "KSampler", "inputs": {}}}
        calls = patch_request(monkeypatch, FakeResponse(200, {"prompt_id": "abc-123", "number": 7}))
        assert client.queue_prompt(workflow) == "abc-123"
        call = calls[0]
        assert call["method"] == "POST"
        assert str(call["url"]) == "http://comfy.test/prompt"
        assert call["json"] == {"prompt": workflow, "client_id": client.client_id}

    def test_rejected_workflow_raises_with_the_node_errors(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, {"node_errors": {"3": ["bad node type"]}}))
        with pytest.raises(ComfyUIError) as exc:
            client.queue_prompt({"3": {}})
        assert "did not accept the workflow" in str(exc.value)
        assert "bad node type" in exc.value.detail

    def test_payload_without_a_prompt_id_raises(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, {}))
        with pytest.raises(ComfyUIError):
            client.queue_prompt({"3": {}})


# ---------------------------------------------------------------------------
# Polling history
# ---------------------------------------------------------------------------
class TestHistory:
    def test_returns_the_entry_for_that_prompt(self, client, monkeypatch):
        entry = {"status": {"completed": True}, "outputs": {"9": {"images": []}}}
        calls = patch_request(monkeypatch, FakeResponse(200, {"pid-1": entry}))
        assert client.history("pid-1") == entry
        assert str(calls[0]["url"]) == "http://comfy.test/history/pid-1"

    def test_missing_entry_gives_an_empty_dict(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, {}))
        assert client.history("pid-1") == {}

    def test_null_entry_gives_an_empty_dict(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, {"pid-1": None}))
        assert client.history("pid-1") == {}

    def test_non_dict_result_gives_an_empty_dict(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, ["nope"]))
        assert client.history("pid-1") == {}


# ---------------------------------------------------------------------------
# Fetching an output file
# ---------------------------------------------------------------------------
class TestViewImage:
    def test_returns_the_raw_bytes_with_view_params(self, client, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(200, content=b"\x89PNG fake bytes"))
        assert client.view_image("out.png", subfolder="runs") == b"\x89PNG fake bytes"
        call = calls[0]
        assert str(call["url"]) == "http://comfy.test/view"
        assert call["params"] == {"filename": "out.png", "subfolder": "runs", "type": "output"}

    def test_folder_type_is_forwarded(self, client, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(200, content=b"x"))
        client.view_image("in.png", folder_type="input")
        assert cast("dict[str, str]", calls[0]["params"])["type"] == "input"


# ---------------------------------------------------------------------------
# Uploading an image
# ---------------------------------------------------------------------------
class TestUploadImage:
    def test_posts_the_file_with_overwrite_true(self, client, monkeypatch):
        result = {"name": "in.png", "subfolder": "", "type": "input"}
        calls = patch_request(monkeypatch, FakeResponse(200, result))
        assert client.upload_image("in.png", b"pixel data") == result
        call = calls[0]
        assert str(call["url"]) == "http://comfy.test/upload/image"
        assert call["files"] == {"image": ("in.png", b"pixel data")}
        assert call["data"] == {"overwrite": "true"}

    def test_overwrite_false_is_lowercased(self, client, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(200, {}))
        client.upload_image("in.png", b"x", overwrite=False)
        assert calls[0]["data"] == {"overwrite": "false"}

    def test_non_dict_result_gives_an_empty_dict(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, ["nope"]))
        assert client.upload_image("in.png", b"x") == {}


# ---------------------------------------------------------------------------
# Queue state and object info
# ---------------------------------------------------------------------------
class TestQueueState:
    def test_returns_the_queue_payload(self, client, monkeypatch):
        payload: dict[str, list[object]] = {"queue_running": [], "queue_pending": []}
        calls = patch_request(monkeypatch, FakeResponse(200, payload))
        assert client.queue_state() == payload
        assert str(calls[0]["url"]) == "http://comfy.test/queue"

    def test_non_dict_result_gives_an_empty_dict(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, ["nope"]))
        assert client.queue_state() == {}


class TestObjectInfo:
    def test_without_a_class_hits_the_root_path(self, client, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(200, {"KSampler": {}}))
        assert client.object_info() == {"KSampler": {}}
        assert str(calls[0]["url"]) == "http://comfy.test/object_info"

    def test_with_a_class_appends_it_to_the_path(self, client, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(200, {"LoraLoader": {}}))
        assert client.object_info("LoraLoader") == {"LoraLoader": {}}
        assert str(calls[0]["url"]) == "http://comfy.test/object_info/LoraLoader"

    def test_non_dict_result_gives_an_empty_dict(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, ["nope"]))
        assert client.object_info() == {}


# ---------------------------------------------------------------------------
# Model discovery through the combo options of a node class
# ---------------------------------------------------------------------------
class TestModelDiscovery:
    def test_list_loras_reads_the_combo_options(self, client, monkeypatch):
        payload = {"LoraLoader": {"input": {"required": {"lora_name": [["a.safetensors", "b.safetensors"]]}}}}
        patch_request(monkeypatch, FakeResponse(200, payload))
        assert client.list_loras() == ["a.safetensors", "b.safetensors"]

    def test_list_checkpoints_uses_the_checkpoint_node(self, client, monkeypatch):
        payload = {"CheckpointLoaderSimple": {"input": {"required": {"ckpt_name": [["v1-5-pruned.ckpt"]]}}}}
        calls = patch_request(monkeypatch, FakeResponse(200, payload))
        assert client.list_checkpoints() == ["v1-5-pruned.ckpt"]
        assert str(calls[0]["url"]) == "http://comfy.test/object_info/CheckpointLoaderSimple"

    def test_options_are_stringified(self, client, monkeypatch):
        payload = {"LoraLoader": {"input": {"required": {"lora_name": [[1, 2.5]]}}}}
        patch_request(monkeypatch, FakeResponse(200, payload))
        assert client.list_loras() == ["1", "2.5"]

    def test_a_plain_spec_gives_no_options(self, client, monkeypatch):
        payload = {"LoraLoader": {"input": {"required": {"lora_name": "not a combo"}}}}
        patch_request(monkeypatch, FakeResponse(200, payload))
        assert client.list_loras() == []

    def test_a_single_level_list_gives_no_options(self, client, monkeypatch):
        payload = {"LoraLoader": {"input": {"required": {"lora_name": ["a.safetensors"]}}}}
        patch_request(monkeypatch, FakeResponse(200, payload))
        assert client.list_loras() == []

    def test_missing_input_gives_no_options(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, {}))
        assert client.list_checkpoints() == []


# ---------------------------------------------------------------------------
# Timeouts and HTTP errors that become ComfyUIError
# ---------------------------------------------------------------------------
class TestTimeoutsAndHttpErrors:
    def test_timeout_becomes_a_504(self, client, monkeypatch):
        patch_request(monkeypatch, requests.Timeout("read timed out"))
        with pytest.raises(ComfyUIError) as exc:
            client.queue_state()
        assert exc.value.status_code == 504
        assert "timed out after 30s" in str(exc.value)
        assert exc.value.detail == "read timed out"

    def test_connection_error_becomes_a_503(self, client, monkeypatch):
        patch_request(monkeypatch, requests.ConnectionError("connection refused"))
        with pytest.raises(ComfyUIError) as exc:
            client.queue_state()
        assert exc.value.status_code == 503
        assert "not reachable" in str(exc.value)

    @pytest.mark.parametrize("status", [400, 404, 500])
    def test_http_errors_become_a_502(self, client, monkeypatch, status):
        patch_request(monkeypatch, FakeResponse(status, {"error": "boom"}))
        with pytest.raises(ComfyUIError) as exc:
            client.queue_state()
        assert exc.value.status_code == 502
        assert f"({status})" in str(exc.value)

    def test_malformed_json_becomes_an_error(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, content=b"<html>oops</html>", text="<html>oops</html>"))
        with pytest.raises(ComfyUIError) as exc:
            client.queue_state()
        assert "malformed response" in str(exc.value)

    def test_empty_body_gives_an_empty_dict(self, client, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, content=b""))
        assert client.queue_state() == {}


# ---------------------------------------------------------------------------
# Error detail extraction from an error answer
# ---------------------------------------------------------------------------
class TestErrorExtraction:
    def _detail_of(self, client, monkeypatch, response):
        patch_request(monkeypatch, response)
        with pytest.raises(ComfyUIError) as exc:
            client.queue_state()
        return exc.value.detail

    def test_error_dict_message_wins(self, client, monkeypatch):
        body = {"error": {"message": "out of memory", "type": "oom"}}
        assert self._detail_of(client, monkeypatch, FakeResponse(500, body)) == "out of memory"

    def test_error_dict_without_message_is_stringified(self, client, monkeypatch):
        body = {"error": {"type": "oom"}}
        assert self._detail_of(client, monkeypatch, FakeResponse(500, body)) == "{'type': 'oom'}"

    def test_plain_error_value_is_stringified(self, client, monkeypatch):
        body = {"error": "bad request"}
        assert self._detail_of(client, monkeypatch, FakeResponse(500, body)) == "bad request"

    def test_node_errors_are_reported(self, client, monkeypatch):
        body = {"node_errors": {"3": ["x"]}}
        assert self._detail_of(client, monkeypatch, FakeResponse(500, body)) == "{'3': ['x']}"

    def test_fallback_is_the_whole_body(self, client, monkeypatch):
        body = {"other": 1}
        assert self._detail_of(client, monkeypatch, FakeResponse(500, body)) == "{'other': 1}"

    def test_non_json_body_uses_the_text(self, client, monkeypatch):
        response = FakeResponse(500, content=b"plain text", text="plain text")
        assert self._detail_of(client, monkeypatch, response) == "plain text"


# ---------------------------------------------------------------------------
# The error type itself
# ---------------------------------------------------------------------------
class TestComfyUIError:
    def test_keeps_message_status_and_detail(self):
        err = ComfyUIError("boom", status_code=418, detail="teapot")
        assert str(err) == "boom"
        assert err.message == "boom"
        assert err.status_code == 418
        assert err.detail == "teapot"

    def test_defaults_to_502_and_a_message_detail(self):
        err = ComfyUIError("boom")
        assert err.status_code == 502
        assert err.detail == "boom"



