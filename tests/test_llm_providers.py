"""LLM provider layer beyond the OpenRouter client (item 19 of docs/QUALITY.md).

tests/test_openrouter.py pins down ``OpenAICompatibleProvider`` in depth; this
module covers what it does not:

* **provider selection** from ``settings.llm_provider`` and the test override,
* **OllamaProvider**, the local backend (complete / chat / vision / health),
* **DeterministicProvider**, the scripted stand-in every API test runs on,
* **usage context attribution**: which organization a usage row belongs to,
* **serialization helpers** (ToolCall / Message) and the small private
  helpers around error detail and Retry-After parsing.

Nothing here touches the network: ``requests`` is monkeypatched everywhere it
is used, exactly like in test_openrouter.py.
"""

from __future__ import annotations

import contextvars
import json
import time

import pytest
import requests

from app.core.config import settings
from app.llm import (
    ChatResponse,
    DeterministicProvider,
    LLMError,
    LLMProvider,
    Message,
    OllamaProvider,
    OpenAICompatibleProvider,
    ToolCall,
    _error_detail,
    _parse_retry_after,
    clear_usage_context,
    current_usage_org_id,
    current_usage_user_id,
    get_llm_provider,
    set_llm_override,
    set_usage_context,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeResponse:
    """Just enough of ``requests.Response`` for the provider code paths."""

    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers = headers or {}

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def patch_request(monkeypatch, *outcomes) -> list[dict]:
    """Replace ``requests.request``; later calls reuse the last outcome."""
    calls = []

    def fake(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return outcomes[min(len(calls) - 1, len(outcomes) - 1)]

    monkeypatch.setattr(requests, "request", fake)
    return calls


def patch_post(monkeypatch, *outcomes) -> list[dict]:
    """Replace ``requests.post`` (Ollama's transport)."""
    calls = []

    def fake(url, json=None, timeout=None):  # noqa: A002 - mirrors requests' signature
        calls.append({"url": url, "json": json, "timeout": timeout})
        return outcomes[min(len(calls) - 1, len(outcomes) - 1)]

    monkeypatch.setattr(requests, "post", fake)
    return calls


def patch_get(monkeypatch, *outcomes) -> list[dict]:
    """Replace ``requests.get`` (Ollama's health/model probes)."""
    calls = []

    def fake(url, timeout=None):
        calls.append({"url": url, "timeout": timeout})
        return outcomes[min(len(calls) - 1, len(outcomes) - 1)]

    monkeypatch.setattr(requests, "get", fake)
    return calls


def _completion(content="ok") -> dict:
    return {"choices": [{"message": {"content": content}}]}


# ---------------------------------------------------------------------------
# Provider selection from settings
# ---------------------------------------------------------------------------
class TestProviderSelection:
    def test_test_kind_returns_deterministic(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_provider", "TEST")  # case-insensitive
        assert isinstance(get_llm_provider(), DeterministicProvider)

    @pytest.mark.parametrize("kind", ["openai_compatible", "openai"])
    def test_openai_kinds_return_the_http_client(self, monkeypatch, kind):
        monkeypatch.setattr(settings, "llm_provider", kind)
        assert isinstance(get_llm_provider(), OpenAICompatibleProvider)

    @pytest.mark.parametrize("kind", ["ollama", "mistral"])  # unknown -> local fallback
    def test_ollama_and_unknown_kinds_return_local(self, monkeypatch, kind):
        monkeypatch.setattr(settings, "llm_provider", kind)
        assert isinstance(get_llm_provider(), OllamaProvider)

    def test_override_wins_over_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
        stub = DeterministicProvider()
        set_llm_override(stub)
        try:
            assert get_llm_provider() is stub
        finally:
            set_llm_override(None)

    def test_clearing_the_override_falls_back_to_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "llm_provider", "openai_compatible")
        set_llm_override(DeterministicProvider())
        try:
            assert isinstance(get_llm_provider(), DeterministicProvider)  # the override
        finally:
            set_llm_override(None)
        assert isinstance(get_llm_provider(), OpenAICompatibleProvider)


# ---------------------------------------------------------------------------
# Usage context (which org/user a usage row is attributed to)
# ---------------------------------------------------------------------------
class TestUsageContext:
    def test_set_read_clear(self):
        # Capture the ambient values first: ContextVars are process-global, so
        # clearing must restore whatever was there before, not assume None.
        org_before = current_usage_org_id()
        user_before = current_usage_user_id()
        tokens = set_usage_context(7, 42)
        try:
            assert (current_usage_org_id(), current_usage_user_id()) == (7, 42)
        finally:
            clear_usage_context(tokens)
        assert (current_usage_org_id(), current_usage_user_id()) == (org_before, user_before)

    def test_clear_accepts_none_and_empty(self):
        clear_usage_context(None)
        clear_usage_context([])

    @pytest.mark.xfail(
        strict=True,
        reason="real bug, see sparton-quality-3/BUGS.md #5",
    )
    def test_resetting_an_already_consumed_token_is_swallowed(self):
        org_before = current_usage_org_id()
        tokens = set_usage_context(1, 2)
        clear_usage_context(tokens)
        clear_usage_context(tokens)  # already-consumed tokens must be swallowed too
        assert current_usage_org_id() == org_before

    def test_resetting_a_token_from_another_context_is_swallowed(self):
        captured: dict = {}

        def in_other_context():
            captured["tokens"] = set_usage_context(3, 4)

        before = (current_usage_org_id(), current_usage_user_id())
        contextvars.copy_context().run(in_other_context)
        clear_usage_context(captured["tokens"])  # ValueError -> swallowed
        assert (current_usage_org_id(), current_usage_user_id()) == before

    def test_usage_row_is_attributed_to_the_calling_context(self, monkeypatch, db_session):
        from sqlalchemy import select

        from app.core.database.identity import Organization
        from app.core.database.usage_models import LLMUsage

        # A real row: SQLite enforces the FK on llm_usage.organization_id.
        org = Organization(name="Context Org")
        db_session.add(org)
        db_session.commit()

        provider = OpenAICompatibleProvider(
            base_url="https://llm.test/v1", api_key="k", model="m/x",
            max_retries=0, sleep=lambda s: None,
        )
        patch_request(monkeypatch, FakeResponse(200, _completion()))
        tokens = set_usage_context(org.id, 99)
        try:
            provider.complete([{"role": "user", "content": "x"}], task="report")
        finally:
            clear_usage_context(tokens)

        row = db_session.execute(select(LLMUsage)).scalars().one()
        assert (row.organization_id, row.user_id) == (org.id, 99)

    def test_usage_row_without_context_is_unattributed(self, monkeypatch, db_session):
        from sqlalchemy import select

        from app.core.database.usage_models import LLMUsage

        provider = OpenAICompatibleProvider(
            base_url="https://llm.test/v1", api_key="k", model="m/x",
            max_retries=0, sleep=lambda s: None,
        )
        patch_request(monkeypatch, FakeResponse(200, _completion()))
        provider.complete([{"role": "user", "content": "x"}])

        row = db_session.execute(select(LLMUsage)).scalars().one()
        assert row.organization_id is None and row.user_id is None


# ---------------------------------------------------------------------------
# Serialization helpers (ToolCall / Message)
# ---------------------------------------------------------------------------
class TestToolCall:
    def test_roundtrip_with_dict_arguments(self):
        tc = ToolCall(id="c1", name="a.b", arguments={"x": 1})
        assert ToolCall.from_dict(tc.to_dict()) == tc

    def test_string_arguments_are_parsed(self):
        raw = {"id": "c2", "name": "n", "arguments": '{"x": [1, 2]}'}
        assert ToolCall.from_dict(raw).arguments == {"x": [1, 2]}

    def test_unparseable_string_arguments_become_empty(self):
        tc = ToolCall.from_dict({"id": "c3", "name": "n", "arguments": "{nope"})
        assert tc.arguments == {}

    def test_missing_fields_default_to_blank(self):
        tc = ToolCall.from_dict({})
        assert (tc.id, tc.name, tc.arguments) == ("", "", {})


class TestMessage:
    def test_to_dict_omits_unset_fields(self):
        assert Message(role="user").to_dict() == {"role": "user"}

    def test_full_roundtrip(self):
        msg = Message(
            role="assistant",
            content="hi",
            tool_calls=[ToolCall(id="c1", name="n", arguments={"a": 1})],
            tool_call_id="c1",
            name="n",
        )
        assert Message.from_dict(msg.to_dict()) == msg

    def test_tool_result_is_json_truncated_at_50k(self):
        call = ToolCall(id="c9", name="big.tool", arguments={})
        result = list(range(60_000))  # serializes to well over 50,000 chars
        msg = Message.tool_result(call, result)
        assert msg.role == "tool"
        assert msg.tool_call_id == "c9" and msg.name == "big.tool"
        assert len(msg.content) == 50_000

    def test_tool_result_handles_non_serializable_values(self):
        call = ToolCall(id="c10", name="t", arguments={})
        msg = Message.tool_result(call, {"obj": object()})
        assert isinstance(msg.content, str) and len(msg.content) <= 50_000


# ---------------------------------------------------------------------------
# LLMError + base-class defaults
# ---------------------------------------------------------------------------
class TestLLMError:
    def test_default_status_is_bad_gateway(self):
        err = LLMError("boom")
        assert str(err) == "boom" and err.message == "boom" and err.status_code == 502

    def test_explicit_status_is_kept(self):
        assert LLMError("bad key", status_code=401).status_code == 401


class _StubProvider(LLMProvider):
    """Minimal concrete provider to exercise the base-class defaults."""

    name = "stub"

    def __init__(self, text="stub answer"):
        self.text = text
        self.seen: list[dict] | None = None

    def complete(self, messages, **kwargs) -> str:
        self.seen = messages
        return self.text

    def is_available(self) -> bool:
        return True


class TestBaseClassDefaults:
    def test_chat_maps_onto_complete_without_tools(self):
        stub = _StubProvider()
        response = stub.chat([Message(role="user", content="hi")])
        assert isinstance(response, ChatResponse)
        assert response.content == "stub answer" and response.tool_calls == []
        assert stub.seen == [{"role": "user", "content": "hi"}]

    def test_list_models_is_empty_by_default(self):
        assert _StubProvider().list_models() == []

    def test_vision_raises_a_clear_error(self):
        with pytest.raises(LLMError, match="does not support vision"):
            _StubProvider().vision_complete("p", "b64")


class TestPrivateHelpers:
    def test_error_detail_prefers_the_json_message(self):
        resp = FakeResponse(200, {"error": {"message": "quota exceeded"}})
        assert _error_detail(resp) == "quota exceeded"

    def test_error_detail_falls_back_to_text_for_non_json(self):
        long_text = "x" * 500
        resp = FakeResponse(200, payload=None, text=long_text)
        assert _error_detail(resp) == long_text[:300]

    def test_error_detail_stringifies_payloads_without_an_error_key(self):
        assert "'detail'" in _error_detail(FakeResponse(200, {"detail": "x"}))
        assert _error_detail(FakeResponse(200, ["a", "b"])) == str(["a", "b"])[:300]

    def test_parse_retry_after_numeric(self):
        assert _parse_retry_after("2.5") == 2.5

    @pytest.mark.parametrize("value", [None, "", "   ", "soon"])
    def test_parse_retry_after_unusable_values(self, value):
        assert _parse_retry_after(value) is None

    def test_parse_retry_after_http_date_is_not_parsed(self):
        # Documented: only the delta-seconds form is honored; an HTTP date is
        # "not worth parsing for a retry loop", so it yields no delay at all.
        assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") is None


# ---------------------------------------------------------------------------
# OllamaProvider: complete / chat / vision
# ---------------------------------------------------------------------------
def _ollama(**kwargs) -> OllamaProvider:
    kwargs.setdefault("base_url", "http://ollama.test")
    return OllamaProvider(**kwargs)


class TestOllamaComplete:
    def test_payload_uses_defaults_and_returns_content(self, monkeypatch):
        calls = patch_post(monkeypatch, FakeResponse(200, _completion("local")))
        assert _ollama(default_model="test-model").complete(
            [{"role": "user", "content": "x"}]
        ) == "local"
        body = calls[0]["json"]
        assert calls[0]["url"] == "http://ollama.test/v1/chat/completions"
        assert body["model"] == "test-model"
        assert body["temperature"] == 0.4 and body["stream"] is False
        assert "max_tokens" not in body and "response_format" not in body

    def test_max_tokens_and_json_mode_are_forwarded(self, monkeypatch):
        calls = patch_post(monkeypatch, FakeResponse(200, _completion("{}")))
        _ollama(default_model="test-model").complete(
            [{"role": "user", "content": "x"}], model="other", max_tokens=64, response_format_json=True
        )
        body = calls[0]["json"]
        assert body["model"] == "other" and body["max_tokens"] == 64
        assert body["response_format"] == {"type": "json_object"}

    def test_default_model_comes_from_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "ollama_model", "from-settings")
        calls = patch_post(monkeypatch, FakeResponse(200, _completion()))
        _ollama().complete([{"role": "user", "content": "y"}])  # no explicit model
        assert calls[0]["json"]["model"] == "from-settings"

    def test_unexpected_shape_raises(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(200, {}))
        with pytest.raises(LLMError, match="Unexpected Ollama response shape"):
            _ollama(default_model="m").complete([{"role": "user", "content": "x"}])

    def test_http_error_is_wrapped(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(404))
        with pytest.raises(LLMError, match="Ollama request failed"):
            _ollama(default_model="m").complete([{"role": "user", "content": "x"}])

    def test_network_error_is_wrapped(self, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(requests, "post", boom)
        with pytest.raises(LLMError, match="Ollama request failed"):
            _ollama(default_model="m").complete([{"role": "user", "content": "x"}])


class TestOllamaChat:
    def test_tools_are_forwarded_and_calls_parsed(self, monkeypatch):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "a.b", "arguments": '{"x": 1}'}},
                            # No id: the provider must generate one.
                            {"function": {"name": "c.d", "arguments": {"y": 2}}},
                        ],
                    }
                }
            ]
        }
        calls = patch_post(monkeypatch, FakeResponse(200, payload))
        response = _ollama(default_model="m").chat(
            [Message(role="user", content="go")], tools=[{"type": "function"}]
        )
        assert calls[0]["json"]["tools"] == [{"type": "function"}]
        assert response.content is None
        assert [tc.name for tc in response.tool_calls] == ["a.b", "c.d"]
        assert response.tool_calls[0].arguments == {"x": 1}
        assert len(response.tool_calls[1].id) == 12, "missing id gets a generated one"

    def test_unparseable_arguments_become_empty(self, monkeypatch):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [{"id": "c", "function": {"name": "n", "arguments": "{bad"}}],
                    }
                }
            ]
        }
        patch_post(monkeypatch, FakeResponse(200, payload))
        response = _ollama(default_model="m").chat([Message(role="user", content="go")])
        assert response.tool_calls[0].arguments == {}

    def test_no_tool_calls_key_means_none(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(200, _completion("plain")))
        response = _ollama(default_model="m").chat([Message(role="user", content="hi")])
        assert response.content == "plain" and response.tool_calls == []

    def test_unexpected_shape_raises(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(200, {"nope": True}))
        with pytest.raises(LLMError, match="Unexpected Ollama response shape"):
            _ollama(default_model="m").chat([Message(role="user", content="hi")])


class TestOllamaVision:
    def test_posts_the_image_and_strips_the_caption(self, monkeypatch):
        monkeypatch.setattr(settings, "vision_model", "test-vision-model")
        monkeypatch.setattr(settings, "vision_timeout", 42.5)
        calls = patch_post(monkeypatch, FakeResponse(200, {"message": {"content": "  a cat  "}}))
        assert _ollama().vision_complete("describe", "BASE64==") == "a cat"
        body = calls[0]
        assert body["url"] == "http://ollama.test/api/chat"
        assert body["timeout"] == 42.5
        assert body["json"]["model"] == "test-vision-model"
        assert body["json"]["messages"][0]["images"] == ["BASE64=="]

    def test_unexpected_shape_raises(self, monkeypatch):
        patch_post(monkeypatch, FakeResponse(200, {}))
        with pytest.raises(LLMError, match="Unexpected Ollama vision response"):
            _ollama().vision_complete("describe", "B64")


# ---------------------------------------------------------------------------
# OllamaProvider: health / models (GET endpoints)
# ---------------------------------------------------------------------------
class TestOllamaHealthAndModels:
    def test_is_available_true_on_200(self, monkeypatch):
        patch_get(monkeypatch, FakeResponse(200))
        assert _ollama().is_available() is True

    @pytest.mark.parametrize("status", [404, 503])
    def test_is_available_false_on_http_error(self, monkeypatch, status):
        patch_get(monkeypatch, FakeResponse(status))
        assert _ollama().is_available() is False

    def test_is_available_false_when_unreachable(self, monkeypatch):
        def boom(*args, **kwargs):
            raise requests.ConnectionError("refused")

        monkeypatch.setattr(requests, "get", boom)
        assert _ollama().is_available() is False

    def test_list_models_returns_the_model_names(self, monkeypatch):
        payload = {
            "models": [
                {"name": "llama3:latest", "size": 1024},
                {"name": "mistral"},
                {},  # no name -> dropped
            ]
        }
        patch_get(monkeypatch, FakeResponse(200, payload))
        assert _ollama().list_models() == ["llama3:latest", "mistral"]

    def test_list_models_falls_back_to_the_data_key(self, monkeypatch):
        # OpenAI-style listing: bare model ids under "data".
        patch_get(monkeypatch, FakeResponse(200, {"data": ["a/b", "c/d"]}))
        assert _ollama().list_models() == ["a/b", "c/d"]

    def test_list_models_empty_on_error(self, monkeypatch):
        patch_get(monkeypatch, FakeResponse(500))
        assert _ollama().list_models() == []


# ---------------------------------------------------------------------------
# DeterministicProvider (the scripted stand-in behind every API test)
# ---------------------------------------------------------------------------
class TestDeterministicProvider:
    def test_name_and_availability(self):
        provider = DeterministicProvider()
        assert provider.name == "test" and provider.is_available() is True

    def test_complete_returns_the_fixed_deterministic_text(self):
        messages = [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second question"},
        ]
        provider = DeterministicProvider()
        assert provider.complete(messages) == "Deterministic response."
        # The conversation is recorded so tests can inspect what was asked.
        assert provider.calls == [messages]

    def test_complete_without_user_messages(self):
        assert (
            DeterministicProvider().complete([{"role": "system", "content": "s"}])
            == "Deterministic response."
        )

    def test_scripted_responses_are_consumed_in_order(self):
        provider = DeterministicProvider(scripted_responses=["one", "two"])
        assert provider.complete([{"role": "user", "content": "a"}]) == "one"
        assert provider.complete([{"role": "user", "content": "b"}]) == "two"

    def test_json_mode_returns_the_scripted_decision(self):
        result = DeterministicProvider().complete(
            [{"role": "user", "content": "a"}], response_format_json=True
        )
        assert json.loads(result) == {"action": "respond", "answer": "ok"}

    def test_chat_without_scripts_returns_the_fixed_text(self):
        response = DeterministicProvider().chat(
            [Message(role="user", content="hello"), Message(role="assistant", content="hi")]
        )
        assert isinstance(response, ChatResponse)
        assert response.content == "Deterministic response." and response.tool_calls == []

    def test_chat_scripted_tool_call_decision(self):
        provider = DeterministicProvider(
            scripted_responses=[{"tool": "lookup_sales", "arguments": {"days": 7}}]
        )
        response = provider.chat([Message(role="user", content="go")])
        assert response.content is None
        call = response.tool_calls[0]
        assert len(response.tool_calls) == 1
        assert call.name == "lookup_sales" and call.arguments == {"days": 7}
        assert len(call.id) == 12

    def test_chat_scripted_string_is_the_final_answer(self):
        provider = DeterministicProvider(scripted_responses=["final answer"])
        response = provider.chat([Message(role="user", content="go")])
        assert response.content == "final answer" and response.tool_calls == []

    def test_vision_is_unsupported(self):
        with pytest.raises(LLMError, match="does not support vision"):
            DeterministicProvider().vision_complete("p", "b64")


# ---------------------------------------------------------------------------
# OpenAICompatibleProvider: gaps left by tests/test_openrouter.py
# ---------------------------------------------------------------------------
def _openai(**kwargs) -> OpenAICompatibleProvider:
    kwargs.setdefault("base_url", "https://llm.test/v1")
    kwargs.setdefault("api_key", "k")
    kwargs.setdefault("model", "m/x")
    return OpenAICompatibleProvider(**kwargs)


class TestOpenAIGaps:
    def test_chat_tool_calls_are_parsed(self, monkeypatch):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "n", "arguments": '{"a": 1}'}}
                        ],
                    }
                }
            ]
        }
        patch_request(monkeypatch, FakeResponse(200, payload))
        response = _openai().chat([Message(role="user", content="go")])
        assert response.content is None
        assert [tc.name for tc in response.tool_calls] == ["n"]
        assert response.tool_calls[0].arguments == {"a": 1}

    def test_chat_without_tool_calls_key(self, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, _completion("plain")))
        response = _openai().chat([Message(role="user", content="hi")])
        assert response.content == "plain" and response.tool_calls == []

    def test_list_models_maps_ids(self, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, {"data": [{"id": "a/b"}, {"id": "c/d"}]}))
        assert _openai().list_models() == ["a/b", "c/d"]

    def test_list_models_raises_when_the_endpoint_fails(self, monkeypatch):
        # Documented contract: an empty list would be indistinguishable from
        # "the account has no models", so a failure must raise instead.
        patch_request(monkeypatch, FakeResponse(500))
        with pytest.raises(LLMError, match="failed after 1 attempt"):
            _openai(max_retries=0).list_models()


class TestOpenAIRetryAndErrors:
    def test_connection_error_is_retried(self, monkeypatch):
        calls = []

        def fake(method, url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                raise requests.ConnectionError("connection refused")
            return FakeResponse(200, _completion("ok"))

        monkeypatch.setattr(requests, "request", fake)
        provider = _openai(max_retries=1, sleep=lambda s: None)
        assert provider.complete([{"role": "user", "content": "x"}]) == "ok"
        assert len(calls) == 2

    def test_unexpected_status_is_not_retried(self, monkeypatch):
        # Neither retryable nor a known client error: fail fast with the status.
        calls = patch_request(monkeypatch, FakeResponse(402, {"detail": "payment required"}))
        provider = _openai(max_retries=3, sleep=lambda s: None)
        with pytest.raises(LLMError, match="Unexpected LLM status 402"):
            provider.complete([{"role": "user", "content": "x"}])
        assert len(calls) == 1


class TestOpenAIPayloadAndShapes:
    def test_chat_forwards_tools(self, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(200, _completion("ok")))
        provider = _openai(max_retries=0)
        provider.chat([Message(role="user", content="go")], tools=[{"type": "function"}])
        assert calls[0]["json"]["tools"] == [{"type": "function"}]

    def test_chat_with_unexpected_shape_raises(self, monkeypatch):
        # chat() has its own shape check; complete's is pinned in test_openrouter.py.
        patch_request(monkeypatch, FakeResponse(200, {"nope": True}))
        with pytest.raises(LLMError, match="Unexpected LLM response shape"):
            _openai(max_retries=0).chat([Message(role="user", content="hi")])


class TestOpenAIModelsAndPricing:
    def test_is_available_false_when_the_cached_list_is_empty(self):
        # A fresh cache short-circuits the probe; an empty model list means
        # "the account has no models", which is not available.
        provider = _openai(max_retries=0)
        provider._models_cache = (time.monotonic(), [])
        assert provider.is_available() is False

    def test_pricing_junk_entries_are_skipped_without_breaking_the_list(self, monkeypatch):
        payload = {
            "data": [
                "not-a-dict",
                {},  # no id -> dropped from the model list too
                {"id": "a/free", "pricing": {"prompt": "free"}},  # unparseable price
                {"id": "b/paid", "pricing": {"prompt": 0.5, "completion": 0.25}},
            ]
        }
        patch_request(monkeypatch, FakeResponse(200, payload))
        provider = _openai(max_retries=0)
        assert provider.list_models() == ["a/free", "b/paid"]
        # Numeric per-token prices scale to the per-million convention.
        assert provider.price_per_million("b/paid") == (500_000.0, 250_000.0)

    def test_price_per_million_unknown_model_triggers_a_listing(self, monkeypatch):
        payload = {"data": [{"id": "b/paid", "pricing": {"prompt": 1.0, "completion": 2.0}}]}
        calls = patch_request(monkeypatch, FakeResponse(200, payload))
        provider = _openai(max_retries=0)
        assert provider.price_per_million("zzz/none") is None
        assert len(calls) == 1  # the listing happened

    def test_price_per_million_none_when_listing_fails(self, monkeypatch):
        patch_request(monkeypatch, FakeResponse(500))
        assert _openai(max_retries=0).price_per_million("b/paid") is None
