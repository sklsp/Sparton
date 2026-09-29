"""Unified LLM provider abstraction (SPARTON AI layer foundation).

Ares' provider pattern (Ollama / OpenAI-compatible / deterministic test)
extended with the chat-completion surface Apollo's features need. The agent
runtime, RAG chat, and vision captioning all go through this one layer.
"""

from __future__ import annotations

import contextvars
import json
import logging
import random
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import requests

from app.core.config import settings
from app.core.observability.metrics import inc

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Usage attribution
# --------------------------------------------------------------------------
# The provider layer has no request object, but usage rows must be attributed to
# an organization for billing. A context variable is the only clean way to
# carry that down from a route handler without threading it through every
# call signature. Workers set it explicitly; anything unset records org NULL.
_usage_org: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "sparton_usage_org", default=None
)
_usage_user: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "sparton_usage_user", default=None
)


def set_usage_context(
    organization_id: int | None, user_id: int | None = None
) -> contextvars.Token:
    """Bind the calling request's organization for usage accounting."""
    return _usage_org.set(organization_id), _usage_user.set(user_id)


def clear_usage_context(tokens) -> None:
    for token in tokens or ():
        try:
            (_usage_org if token.var.name == "sparton_usage_org" else _usage_user).reset(token)
        except (ValueError, LookupError):
            pass


def current_usage_org_id() -> int | None:
    return _usage_org.get()


def current_usage_user_id() -> int | None:
    return _usage_user.get()


def _error_detail(response) -> str:
    """The provider's own error message, which is usually the useful part."""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(payload)[:300]


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header. Only the delta-seconds form."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        # HTTP-date form; not worth parsing for a retry loop.
        return None



@dataclass(slots=True)
class ToolCall:
    """One function-tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ToolCall:
        arguments = raw.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            arguments=dict(arguments),
        )


@dataclass(slots=True)
class Message:
    """One conversation message (serializable onto AgentRun.messages)."""

    role: str
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    # For role == "tool": which call this result answers.
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        raw: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            raw["content"] = self.content
        if self.tool_calls:
            raw["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id is not None:
            raw["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            raw["name"] = self.name
        return raw

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Message:
        return cls(
            role=str(raw.get("role", "user")),
            content=raw.get("content"),
            tool_calls=[ToolCall.from_dict(tc) for tc in raw.get("tool_calls", [])],
            tool_call_id=raw.get("tool_call_id"),
            name=raw.get("name"),
        )

    @classmethod
    def tool_result(cls, call: ToolCall, result: Any) -> Message:
        return cls(
            role="tool",
            content=json.dumps(result, default=str)[:50_000],
            tool_call_id=call.id,
            name=call.name,
        )


@dataclass(slots=True)
class ChatResponse:
    content: str | None
    tool_calls: list[ToolCall]


class LLMError(Exception):
    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class LLMProvider(ABC):
    """One chat-completion contract for every backend."""

    name: str = "base"

    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.4,
        max_tokens: int | None = None,
        response_format_json: bool = False,
        task: str = "generic",
    ) -> str:
        """Return assistant text for a list of {role, content} messages.

        ``task`` labels the call for usage accounting ("agent", "report",
        "extract", ...). Providers that cannot meter tokens ignore it.
        """

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """Conversation with optional function-tool calling.

        Default implementation maps onto ``complete`` (no native tool
        support); OpenAI-compatible backends override it.
        """
        dicts = [m.to_dict() for m in messages]
        return ChatResponse(content=self.complete(dicts), tool_calls=[])

    @abstractmethod
    def is_available(self) -> bool:
        """Cheap health probe used by /health and readiness checks."""

    # Optional capability hooks -------------------------------------------------

    def list_models(self) -> list[str]:
        return []

    def vision_complete(self, prompt: str, image_b64: str) -> str:
        raise LLMError(f"{self.name} provider does not support vision")


class OllamaProvider(LLMProvider):
    """Local Ollama via its OpenAI-compatible endpoint."""

    name = "ollama"

    def __init__(self, base_url: str | None = None, default_model: str | None = None) -> None:
        self.base_url = (base_url or settings.ollama_base_url).rstrip("/")
        self.default_model = default_model or settings.ollama_model

    def _post(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        try:
            response = requests.post(
                f"{self.base_url}{path}",
                json=payload,
                timeout=timeout or settings.llm_timeout_seconds,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.4,
        max_tokens: int | None = None,
        response_format_json: bool = False,
        task: str = "generic",
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if response_format_json:
            payload["response_format"] = {"type": "json_object"}
        data = self._post("/v1/chat/completions", payload)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected Ollama response shape: {data}") from exc

    def vision_complete(self, prompt: str, image_b64: str) -> str:
        """Caption/describe an image via Ollama's native vision API."""
        data = self._post(
            "/api/chat",
            {
                "model": settings.vision_model,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                        "images": [image_b64],
                    }
                ],
                "stream": False,
            },
            timeout=settings.vision_timeout,
        )
        try:
            return data["message"]["content"].strip()
        except (KeyError, TypeError) as exc:
            raise LLMError(f"Unexpected Ollama vision response: {data}") from exc

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        task: str = "agent",
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": [m.to_dict() for m in messages],
            "temperature": 0.4,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        data = self._post("/v1/chat/completions", payload)
        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected Ollama response shape: {data}") from exc

        tool_calls = []
        for raw in choice.get("tool_calls") or []:
            fn = raw.get("function", {})
            call_id = raw.get("id") or uuid.uuid4().hex[:12]
            arguments = fn.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            tool_calls.append(ToolCall(
                id=str(call_id),
                name=str(fn.get("name", "")),
                arguments=dict(arguments),
            ))
        return ChatResponse(content=choice.get("content"), tool_calls=tool_calls)

    def is_available(self) -> bool:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=3)
            return response.ok
        except requests.RequestException:
            return False

    def list_models(self) -> list[str]:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=5)
            response.raise_for_status()
            data = response.json()
            models = data.get("models") or [
                {"name": m} for m in data.get("data", [])
            ]
            return [m.get("name", "") for m in models if m.get("name")]
        except requests.RequestException:
            return []


class OpenAICompatibleProvider(LLMProvider):
    """Any OpenAI-compatible hosted endpoint. Tuned for OpenRouter.

    What a production deployment needs from an LLM HTTP client, all of which
    this class now does:

    * **Identifies itself.** OpenRouter asks every app to send ``HTTP-Referer``
      and ``X-Title``; it uses them for its public leaderboard and to reach a
      human if the key is abused.
    * **Survives a bad minute.** 429 (rate limited / out of credit) and 5xx
      are retried with exponential backoff plus jitter. Other 4xx are *not*
      retried: a 400 is a bug in our request and repeating it just burns money.
    * **Does not hang.** Every request has a hard timeout, with connect and
      read budgets separated so a black-holed endpoint fails fast.
    * **Is honest about money.** The response ``usage`` block is written to
      :class:`~app.core.database.usage_models.LLMUsage`, which is what plan
      limits and the cost-per-customer figure are computed from.
    * **Knows what it can do.** ``is_available()`` really calls ``/models``
      instead of checking that a string is non-empty.
    """

    name = "openai_compatible"

    #: Statuses worth retrying. 429 is rate limiting or exhausted credit; the
    #: 5xx family is the provider having a bad day.
    RETRY_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
    #: Never retried: 4xx means our request is wrong, and repeating it will
    #: produce the same 4xx three seconds from now.
    NON_RETRY_STATUSES = frozenset({400, 401, 403, 404, 405, 413, 422})

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        *,
        site_url: str | None = None,
        app_name: str | None = None,
        max_retries: int | None = None,
        timeout: float | None = None,
        sleep=time.sleep,
    ) -> None:
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.api_key = api_key or settings.openai_api_key or ""
        self.model = model or settings.llm_model_strong or settings.openai_model
        self.cheap_model = settings.llm_model_cheap
        self.site_url = site_url or settings.openrouter_site_url
        self.app_name = app_name or settings.openrouter_app_name
        self.max_retries = settings.llm_max_retries if max_retries is None else max_retries
        self.timeout = timeout or settings.llm_timeout_seconds
        # Injectable so tests can assert on backoff without really sleeping.
        self._sleep = sleep
        #: model id -> (USD/1M prompt, USD/1M completion), from /models.
        self._pricing: dict[str, tuple[float, float]] = {}
        self._models_cache: tuple[float, list[str]] | None = None


    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------
    @property
    def headers(self) -> dict[str, str]:
        """Auth plus the attribution headers OpenRouter asks every app to send."""
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.app_name:
            headers["X-Title"] = self.app_name
        return headers

    def _connect_timeout(self) -> tuple[float, float]:
        """(connect, read) — a black-holed host must not eat the whole budget."""
        return min(10.0, self.timeout), self.timeout

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with FULL jitter, capped.

        Full jitter (uniform over [0, base*2^n]) rather than a fixed delay, so N
        workers hitting a 429 together do not retry in lockstep and re-trigger
        the rate limit every single time.
        """
        ceiling = min(
            settings.llm_retry_cap_seconds,
            settings.llm_retry_base_seconds * (2**attempt),
        )
        return random.uniform(0, max(ceiling, 0.0))

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """One HTTP call with retries. Returns parsed JSON, raises LLMError."""
        url = f"{self.base_url}{path}"
        attempts = max(0, self.max_retries) + 1
        last_error = "no attempt was made"

        for attempt in range(attempts):
            if attempt:
                delay = self._backoff_delay(attempt - 1)
                inc("llm_retry_total", provider=self.name)
                logger.warning(
                    "LLM %s %s retry %d/%d in %.2fs (%s)",
                    method, path, attempt, attempts - 1, delay, last_error,
                )
                self._sleep(delay)
            try:
                response = requests.request(
                    method, url, timeout=self._connect_timeout(), **kwargs
                )
            except requests.Timeout as exc:
                last_error = f"timeout: {exc}"
                continue
            except requests.RequestException as exc:
                # A DNS failure will not fix itself; anything else might.
                last_error = str(exc)
                if isinstance(exc, requests.ConnectionError) and (
                    "name or service not known" in str(exc).lower()
                    or "nodename nor servname" in str(exc).lower()
                ):
                    break
                continue

            status = response.status_code
            if status in self.NON_RETRY_STATUSES:
                inc("llm_requests_total", provider=self.name, outcome="rejected")
                raise LLMError(
                    f"LLM request rejected ({status}): {_error_detail(response)}"
                )
            if status in self.RETRY_STATUSES or status >= 500:
                last_error = f"HTTP {status}: {_error_detail(response)}"
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is not None and attempt < attempts - 1:
                    # The provider knows its own recovery window better than
                    # our backoff curve does.
                    self._sleep(min(retry_after, settings.llm_retry_cap_seconds))
                continue
            if not response.ok:
                inc("llm_requests_total", provider=self.name, outcome="error")
                raise LLMError(f"Unexpected LLM status {status}: {_error_detail(response)}")

            inc("llm_requests_total", provider=self.name, outcome="success")
            try:
                return response.json()
            except ValueError as exc:
                raise LLMError("LLM returned a non-JSON response") from exc

        inc("llm_requests_total", provider=self.name, outcome="failed")
        raise LLMError(f"LLM request failed after {attempts} attempt(s): {last_error}")


    # ------------------------------------------------------------------
    # Token accounting
    # ------------------------------------------------------------------
    def _record_usage(
        self,
        data: dict[str, Any],
        *,
        task: str,
        model: str,
        started: float,
        attempts: int = 1,
    ) -> None:
        """Persist one usage row, best-effort.

        Accounting must never be the reason a customer request fails, so every
        error here is logged and swallowed rather than raised.
        """
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
        # OpenRouter's `pricing` block is USD *per token*, so the raw value is
        # multiplied by the token count. (Not divided by 1e6 — that would be the
        # per-million convention and is off by a factor of a million.)
        price = self._pricing.get(model)
        cost = (
            prompt_tokens * price[0] + completion_tokens * price[1] if price else 0.0
        )

        try:
            from app.core.database.base import SessionLocal
            from app.core.database.usage_models import LLMUsage

            db = SessionLocal()
            try:
                db.add(
                    LLMUsage(
                        organization_id=current_usage_org_id(),
                        user_id=current_usage_user_id(),
                        provider=self.name,
                        model=model,
                        task=task,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        attempts=attempts,
                        cost_usd=cost,
                    )
                )
                db.commit()
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 — never fail a request over accounting
            logger.warning("Could not record LLM usage: %s", exc)

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------
    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.4,
        max_tokens: int | None = None,
        response_format_json: bool = False,
        task: str = "generic",
    ) -> str:
        """Assistant text for a message list. ``model=None`` uses the strong model.

        ``task`` is a label written to the usage row so spend can be attributed
        to a feature ("report", "extract", "chat").
        """
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if response_format_json:
            payload["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        data = self._request(
            "POST", "/chat/completions", json=payload, headers=self.headers
        )
        self._record_usage(data, task=task, model=payload["model"], started=started)

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {str(data)[:200]}") from exc
        return content or ""

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        task: str = "agent",
    ) -> ChatResponse:
        """Conversation with native function-tool calling."""
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": 0.4,
        }
        if tools:
            payload["tools"] = tools

        started = time.monotonic()
        data = self._request(
            "POST", "/chat/completions", json=payload, headers=self.headers
        )
        self._record_usage(data, task=task, model=payload["model"], started=started)

        try:
            choice = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"Unexpected LLM response shape: {str(data)[:200]}") from exc

        tool_calls = []
        for raw in choice.get("tool_calls") or []:
            fn = raw.get("function", {})
            arguments = fn.get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            tool_calls.append(ToolCall(
                id=str(raw.get("id") or uuid.uuid4().hex[:12]),
                name=str(fn.get("name", "")),
                arguments=dict(arguments) if isinstance(arguments, dict) else {},
            ))
        return ChatResponse(content=choice.get("content"), tool_calls=tool_calls)

    def list_models(self) -> list[str]:
        """Model ids this account can route to, via ``GET /models``.

        Raises LLMError when the endpoint cannot be reached — an empty list
        would be indistinguishable from "the account has no models", which is a
        very different operational signal.
        """
        if not self.api_key:
            raise LLMError("No API key configured for the LLM provider")
        data = self._request("GET", "/models", headers=self.headers)
        self._load_pricing(data)
        models = [
            str(entry["id"])
            for entry in (data.get("data") or [])
            if isinstance(entry, dict) and entry.get("id")
        ]
        self._models_cache = (time.monotonic(), models)
        return models

    #: Seconds an availability result is reused before probing again, so a
    #: health check every 10s cannot become a request storm.
    _availability_ttl = 60.0

    def is_available(self) -> bool:
        """A real reachability + credential check, not ``bool(api_key)``."""
        if not self.api_key:
            return False
        if self._models_cache is not None:
            fetched_at, models = self._models_cache
            if time.monotonic() - fetched_at < self._availability_ttl:
                return bool(models)
        try:
            return bool(self.list_models())
        except LLMError as exc:
            logger.warning("LLM availability check failed: %s", exc)
            return False

    def _load_pricing(self, data: dict[str, Any]) -> None:
        """OpenRouter reports USD-per-token prices on ``GET /models``."""
        for entry in data.get("data") or []:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("id")
            if not model_id:
                continue
            pricing = entry.get("pricing") or {}
            try:
                self._pricing[model_id] = (
                    float(pricing.get("prompt") or 0.0),
                    float(pricing.get("completion") or 0.0),
                )
            except (TypeError, ValueError):
                # A model priced in a way we do not understand is simply not
                # costed; it must not break the model listing.
                continue

    def price_per_million(self, model: str) -> tuple[float, float] | None:
        """(prompt, completion) USD per 1M tokens, for human-facing cost figures.

        OpenRouter reports USD per token; the docs/LAUNCH.md cost table is
        quoted per million because that is how providers advertise prices.
        """
        if model not in self._pricing:
            try:
                self.list_models()
            except LLMError:
                return None
        price = self._pricing.get(model)
        if price is None:
            return None
        return (price[0] * 1e6, price[1] * 1e6)


class DeterministicProvider(LLMProvider):
    """Fixture provider for tests/CI — no network, fully predictable.

    Emits a JSON tool-call decision when the last message asks for JSON,
    otherwise echoes a grounded summary of the conversation.
    """

    name = "test"

    def __init__(self, scripted_responses: list[str] | None = None) -> None:
        self.scripted_responses = list(scripted_responses or [])
        self.calls: list[list[dict[str, Any]]] = []

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.4,
        max_tokens: int | None = None,
        response_format_json: bool = False,
        task: str = "generic",
    ) -> str:
        self.calls.append(messages)
        if self.scripted_responses:
            return self.scripted_responses.pop(0)
        if response_format_json:
            return json.dumps({"action": "respond", "answer": "ok"})
        return "Deterministic response."

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        *,
        model: str | None = None,
        task: str = "agent",
    ) -> ChatResponse:
        """Scriptable tool-calling for tests.

        Script entries may be plain strings (final answers) or dicts shaped
        like ``{"tool": name, "arguments": {...}}`` (a tool-call decision).
        """
        self.calls.append([m.to_dict() for m in messages])
        if not self.scripted_responses:
            return ChatResponse(content="Deterministic response.", tool_calls=[])

        entry = self.scripted_responses.pop(0)
        if isinstance(entry, dict) and "tool" in entry:
            call = ToolCall(
                id=uuid.uuid4().hex[:12],
                name=str(entry["tool"]),
                arguments=dict(entry.get("arguments", {})),
            )
            return ChatResponse(content=None, tool_calls=[call])
        return ChatResponse(content=str(entry), tool_calls=[])

    def is_available(self) -> bool:
        return True


_provider_override: LLMProvider | None = None


def set_llm_override(provider: LLMProvider | None) -> None:
    """Test hook: force a specific provider process-wide."""
    global _provider_override
    _provider_override = provider


def get_llm_provider() -> LLMProvider:
    if _provider_override is not None:
        return _provider_override
    kind = settings.llm_provider.lower()
    if kind == "test":
        return DeterministicProvider()
    if kind in ("openai_compatible", "openai"):
        return OpenAICompatibleProvider()
    return OllamaProvider()


__all__ = [
    "ChatResponse",
    "DeterministicProvider",
    "LLMError",
    "LLMProvider",
    "Message",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "ToolCall",
    "clear_usage_context",
    "current_usage_org_id",
    "current_usage_user_id",
    "get_llm_provider",
    "set_llm_override",
    "set_usage_context",
]
