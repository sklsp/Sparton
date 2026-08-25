"""Unified LLM provider abstraction (SPARTON AI layer foundation).

Ares' provider pattern (Ollama / OpenAI-compatible / deterministic test)
extended with the chat-completion surface Apollo's features need. The agent
runtime, RAG chat, and vision captioning all go through this one layer.
"""

from __future__ import annotations

import json
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)


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
    ) -> str:
        """Return assistant text for a list of {role, content} messages."""

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
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": self.default_model,
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
    """Any OpenAI-compatible hosted endpoint."""

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        self.api_key = api_key or settings.openai_api_key or ""
        self.model = model or settings.openai_model

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        temperature: float = 0.4,
        max_tokens: int | None = None,
        response_format_json: bool = False,
    ) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if response_format_json:
            payload["response_format"] = {"type": "json_object"}
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=settings.llm_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]
        except requests.RequestException as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Unexpected LLM response shape") from exc

    def is_available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_dict() for m in messages],
            "temperature": 0.4,
        }
        if tools:
            payload["tools"] = tools
        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=settings.llm_timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
            choice = data["choices"][0]["message"]
        except requests.RequestException as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("Unexpected LLM response shape") from exc

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
                arguments=dict(arguments),
            ))
        return ChatResponse(content=choice.get("content"), tool_calls=tool_calls)


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
    "get_llm_provider",
    "set_llm_override",
]
