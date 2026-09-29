"""OpenRouter provider tests. Every HTTP call is mocked — no network, no key.

Covers the production requirements from docs/DECISIONS.md D-006 and D-007:
attribution headers, retry/backoff policy, timeouts, token accounting, model
listing, and a real availability check.
"""

from __future__ import annotations

import json

import pytest
import requests

from app.llm import LLMError, Message, OpenAICompatibleProvider


class _Invalid:
    """Sentinel that makes FakeResponse.json() raise, like a real bad body."""


_INVALID = _Invalid()


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, payload=None, headers=None, text=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text if text is not None else json.dumps(self._payload, default=str)

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is _INVALID:
            raise ValueError("not json")
        return self._payload


def completion(content="hello", usage=None, model="test/model"):
    return {
        "id": "gen-1",
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": usage
        or {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }


def make_provider(**overrides):
    """A provider with a recording sleep, so backoff costs no wall time."""
    slept: list[float] = []
    kwargs = {
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "sk-or-test-key",
        "model": "strong/model",
        "site_url": "https://sparton.test",
        "app_name": "Sparton Intelligence",
        "max_retries": 3,
        "sleep": slept.append,
    }
    kwargs.update(overrides)
    provider = OpenAICompatibleProvider(**kwargs)
    provider.slept = slept
    return provider


@pytest.fixture()
def provider():
    return make_provider()


def patch_request(monkeypatch, *responses):
    """Make requests.request return `responses` in order; record the calls."""
    calls: list[dict] = []
    queue = list(responses)

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        item = queue.pop(0) if queue else responses[-1]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests, "request", fake_request)
    return calls


# ---------------------------------------------------------------------------
# Attribution headers (an OpenRouter requirement)
# ---------------------------------------------------------------------------
class TestAttributionHeaders:
    def test_sends_referer_and_title(self, provider, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(200, completion()))
        provider.complete([{"role": "user", "content": "hi"}])
        headers = calls[0]["headers"]
        assert headers["HTTP-Referer"] == "https://sparton.test"
        assert headers["X-Title"] == "Sparton Intelligence"
        assert headers["Authorization"] == "Bearer sk-or-test-key"

    def test_headers_omitted_when_not_configured(self):
        # Settings fill in defaults, so the constructor args have to be able to
        # win: an empty site_url/app_name must produce no attribution headers.
        from app.core.config import settings

        saved = (settings.openrouter_site_url, settings.openrouter_app_name)
        settings.openrouter_site_url = ""
        settings.openrouter_app_name = ""
        try:
            p = OpenAICompatibleProvider(base_url="https://x.test/v1", api_key="k")
            assert "HTTP-Referer" not in p.headers
            assert "X-Title" not in p.headers
        finally:
            settings.openrouter_site_url, settings.openrouter_app_name = saved


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------
class TestRetryPolicy:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
    def test_retries_transient_statuses(self, provider, monkeypatch, status):
        calls = patch_request(
            monkeypatch,
            FakeResponse(status, {"error": "later"}),
            FakeResponse(200, completion()),
        )
        assert provider.complete([{"role": "user", "content": "x"}]) == "hello"
        assert len(calls) == 2, f"status {status} should be retried"

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_does_not_retry_client_errors(self, provider, monkeypatch, status):
        """A 400 is our bug; repeating it three times just costs money."""
        calls = patch_request(
            monkeypatch, FakeResponse(status, {"error": {"message": "nope"}})
        )
        with pytest.raises(LLMError) as exc:
            provider.complete([{"role": "user", "content": "x"}])
        assert len(calls) == 1, f"status {status} must not be retried"
        assert "nope" in str(exc.value)

    def test_retries_timeouts(self, provider, monkeypatch):
        calls = patch_request(
            monkeypatch, requests.Timeout("read timed out"), FakeResponse(200, completion())
        )
        assert provider.complete([{"role": "user", "content": "x"}]) == "hello"
        assert len(calls) == 2

    def test_gives_up_after_max_retries(self, provider, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(503, {"error": "down"}))
        with pytest.raises(LLMError) as exc:
            provider.complete([{"role": "user", "content": "x"}])
        assert len(calls) == 4, "1 initial attempt + 3 retries"
        assert "4 attempt" in str(exc.value)

    def test_backoff_is_jittered_and_bounded(self, provider, monkeypatch):
        patch_request(monkeypatch, FakeResponse(503, {}), FakeResponse(200, completion()))
        provider.complete([{"role": "user", "content": "x"}])
        assert len(provider.slept) == 1
        assert 0.0 <= provider.slept[0] <= 30.0

    def test_honours_retry_after(self, provider, monkeypatch):
        patch_request(
            monkeypatch,
            FakeResponse(429, {"error": "slow down"}, headers={"Retry-After": "7"}),
            FakeResponse(200, completion()),
        )
        provider.complete([{"role": "user", "content": "x"}])
        assert 7.0 in provider.slept

    def test_retry_after_is_capped(self, provider, monkeypatch):
        patch_request(
            monkeypatch,
            FakeResponse(429, {}, headers={"Retry-After": "9999"}),
            FakeResponse(200, completion()),
        )
        provider.complete([{"role": "user", "content": "x"}])
        assert max(provider.slept) <= 30.0

    def test_dns_failure_is_not_retried(self, provider, monkeypatch):
        calls = patch_request(
            monkeypatch,
            requests.ConnectionError("HTTPSConnectionPool: Name or service not known"),
        )
        with pytest.raises(LLMError):
            provider.complete([{"role": "user", "content": "x"}])
        assert len(calls) == 1, "DNS will not fix itself; stop immediately"


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------
class TestTimeouts:
    def test_passes_connect_and_read_budgets(self, provider, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(200, completion()))
        provider.complete([{"role": "user", "content": "x"}])
        connect, read = calls[0]["timeout"]
        assert connect <= 10.0, "a black-holed host must fail fast"
        assert read == provider.timeout

    def test_read_budget_follows_configuration(self):
        p = make_provider(timeout=45.0)
        assert p._connect_timeout() == (10.0, 45.0)


# ---------------------------------------------------------------------------
# Token accounting (billing depends on this)
# ---------------------------------------------------------------------------
class TestUsageAccounting:
    def test_records_tokens_per_call(self, provider, monkeypatch, db_session):
        from sqlalchemy import select

        from app.core.database.usage_models import LLMUsage

        patch_request(
            monkeypatch,
            FakeResponse(
                200,
                completion(usage={
                    "prompt_tokens": 1200,
                    "completion_tokens": 340,
                    "total_tokens": 1540,
                }),
            ),
        )
        provider.complete([{"role": "user", "content": "x"}], task="report")

        rows = db_session.execute(select(LLMUsage)).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert (row.prompt_tokens, row.completion_tokens, row.total_tokens) == (1200, 340, 1540)
        assert row.task == "report"
        assert row.model == "strong/model"
        assert row.provider == "openai_compatible"
        assert row.latency_ms >= 0

    def test_records_usage_for_tool_calling_too(self, provider, monkeypatch, db_session):
        from sqlalchemy import select

        from app.core.database.usage_models import LLMUsage

        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "call_1", "function": {"name": "x.y", "arguments": '{"a": 1}'}}
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        patch_request(monkeypatch, FakeResponse(200, payload))
        provider.chat([Message(role="user", content="do a thing")])

        rows = db_session.execute(select(LLMUsage)).scalars().all()
        assert len(rows) == 1
        assert rows[0].task == "agent"

    def test_missing_usage_block_records_zero_not_raises(
        self, provider, monkeypatch, db_session
    ):
        from sqlalchemy import select

        from app.core.database.usage_models import LLMUsage

        patch_request(
            monkeypatch, FakeResponse(200, {"choices": [{"message": {"content": "x"}}]})
        )
        assert provider.complete([{"role": "user", "content": "x"}]) == "x"
        rows = db_session.execute(select(LLMUsage)).scalars().all()
        assert len(rows) == 1 and rows[0].total_tokens == 0

    def test_cost_is_computed_from_reported_pricing(self, provider, monkeypatch, db_session):
        from sqlalchemy import select

        from app.core.database.usage_models import LLMUsage

        # OpenRouter's pricing block is USD per token: $3/1M prompt, $15/1M completion.
        provider._pricing["strong/model"] = (3.0 / 1e6, 15.0 / 1e6)
        patch_request(
            monkeypatch,
            FakeResponse(
                200,
                completion(usage={
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 1_000_000,
                    "total_tokens": 2_000_000,
                }),
            ),
        )
        provider.complete([{"role": "user", "content": "x"}])
        row = db_session.execute(select(LLMUsage)).scalars().one()
        assert row.cost_usd == pytest.approx(18.0)

    def test_accounting_failure_does_not_break_the_request(self, provider, monkeypatch):
        """Billing must never be the reason a customer request fails."""
        import app.core.database.usage_models as usage_module

        patch_request(monkeypatch, FakeResponse(200, completion()))
        original = usage_module.LLMUsage

        class Exploding:
            def __init__(self, **kwargs):
                raise RuntimeError("database is down")

        usage_module.LLMUsage = Exploding
        try:
            assert provider.complete([{"role": "user", "content": "x"}]) == "hello"
        finally:
            usage_module.LLMUsage = original


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------
class TestModelRouting:
    def test_default_model_is_the_strong_one(self, provider):
        assert provider.model == "strong/model"

    def test_explicit_model_overrides(self, provider, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(200, completion()))
        provider.complete([{"role": "user", "content": "x"}], model="cheap/model")
        assert calls[0]["json"]["model"] == "cheap/model"

    def test_records_the_model_actually_used(self, provider, monkeypatch, db_session):
        from sqlalchemy import select

        from app.core.database.usage_models import LLMUsage

        patch_request(monkeypatch, FakeResponse(200, completion()))
        provider.complete([{"role": "user", "content": "x"}], model="cheap/model")
        assert db_session.execute(select(LLMUsage)).scalars().one().model == "cheap/model"

    def test_json_mode_and_max_tokens_are_forwarded(self, provider, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(200, completion()))
        provider.complete(
            [{"role": "user", "content": "x"}], max_tokens=512, response_format_json=True
        )
        body = calls[0]["json"]
        assert body["max_tokens"] == 512
        assert body["response_format"] == {"type": "json_object"}


# ---------------------------------------------------------------------------
# Model discovery + a real availability check
# ---------------------------------------------------------------------------
MODELS_PAYLOAD = {
    "data": [
        {
            "id": "anthropic/claude-3.5-sonnet",
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        },
        {
            "id": "google/gemini-2.0-flash-001",
            "pricing": {"prompt": "0.0000001", "completion": "0.0000004"},
        },
    ]
}


class TestDiscovery:
    def test_list_models(self, provider, monkeypatch):
        calls = patch_request(monkeypatch, FakeResponse(200, MODELS_PAYLOAD))
        assert "anthropic/claude-3.5-sonnet" in provider.list_models()
        assert calls[0]["url"] == "https://openrouter.ai/api/v1/models"

    def test_pricing_is_parsed_for_cost_accounting(self, provider, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, MODELS_PAYLOAD))
        provider.list_models()
        assert provider.price_per_million("anthropic/claude-3.5-sonnet") == pytest.approx(
            (3.0, 15.0)
        )

    def test_is_available_is_a_real_check(self, provider, monkeypatch):
        """Not `bool(api_key)` — it must actually reach the endpoint."""
        calls = patch_request(monkeypatch, FakeResponse(200, MODELS_PAYLOAD))
        assert provider.is_available() is True
        assert len(calls) == 1, "is_available must contact /models"

    def test_is_available_false_on_auth_failure(self, provider, monkeypatch):
        patch_request(monkeypatch, FakeResponse(401, {"error": {"message": "bad key"}}))
        assert provider.is_available() is False

    def test_is_available_false_on_network_failure(self, provider, monkeypatch):
        patch_request(monkeypatch, requests.ConnectionError("refused"))
        assert provider.is_available() is False

    def test_is_available_false_without_a_key(self):
        assert make_provider(api_key="").is_available() is False

    def test_availability_result_is_cached(self, provider, monkeypatch):
        """A health probe every 10s must not become a request storm."""
        calls = patch_request(monkeypatch, FakeResponse(200, MODELS_PAYLOAD))
        assert provider.is_available() is True
        assert provider.is_available() is True
        assert provider.is_available() is True
        assert len(calls) == 1

    def test_list_models_without_a_key_raises(self):
        with pytest.raises(LLMError):
            make_provider(api_key="").list_models()


# ---------------------------------------------------------------------------
# Response handling
# ---------------------------------------------------------------------------
class TestResponseHandling:
    def test_malformed_json_raises_llm_error(self, provider, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, _INVALID, text="<html>oops</html>"))
        with pytest.raises(LLMError) as exc:
            provider.complete([{"role": "user", "content": "x"}])
        assert "non-JSON" in str(exc.value)

    def test_unexpected_shape_raises_llm_error(self, provider, monkeypatch):
        patch_request(monkeypatch, FakeResponse(200, {"choices": []}))
        with pytest.raises(LLMError) as exc:
            provider.complete([{"role": "user", "content": "x"}])
        assert "response shape" in str(exc.value)

    def test_null_content_becomes_empty_string(self, provider, monkeypatch):
        patch_request(
            monkeypatch, FakeResponse(200, {"choices": [{"message": {"content": None}}]})
        )
        assert provider.complete([{"role": "user", "content": "x"}]) == ""

    def test_string_tool_arguments_are_parsed(self, provider, monkeypatch):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "t", "arguments": '{"k": "v"}'}}
                        ],
                    }
                }
            ]
        }
        patch_request(monkeypatch, FakeResponse(200, payload))
        call = provider.chat([Message(role="user", content="x")]).tool_calls[0]
        assert call.arguments == {"k": "v"}

    def test_unparseable_tool_arguments_become_empty(self, provider, monkeypatch):
        payload = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "t", "arguments": "not json {"}}
                        ],
                    }
                }
            ]
        }
        patch_request(monkeypatch, FakeResponse(200, payload))
        call = provider.chat([Message(role="user", content="x")]).tool_calls[0]
        assert call.arguments == {}

    def test_trailing_slash_in_base_url_is_normalised(self):
        p = make_provider(base_url="https://openrouter.ai/api/v1/")
        assert p.base_url == "https://openrouter.ai/api/v1"


# ---------------------------------------------------------------------------
# No secrets in source
# ---------------------------------------------------------------------------
def test_no_api_key_is_hardcoded_in_source():
    """Keys come from the environment, never from the repository."""
    for path in ("app/llm.py", "app/core/config.py"):
        source = open(path, encoding="utf-8").read()
        assert "sk-or-" not in source, f"a literal OpenRouter key is in {path}"
        assert "sk-" not in source, f"a literal API key is in {path}"
