"""Tool execution grounding.

Ported from Ares `app/agent/grounding.py` (unchanged behavior). Guarantees
the agent cannot present fabricated facts after a tool failure — enforced in
code, not prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class ToolState(StrEnum):
    TOOL_REQUESTED = "TOOL_REQUESTED"
    VALIDATING = "VALIDATING"
    VALID = "VALID"
    INVALID = "INVALID"
    RETRY_REQUIRED = "RETRY_REQUIRED"
    EXECUTING = "EXECUTING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


TRANSITIONS: dict[ToolState, set[ToolState]] = {
    ToolState.TOOL_REQUESTED: {ToolState.VALIDATING},
    ToolState.VALIDATING: {ToolState.VALID, ToolState.INVALID},
    ToolState.INVALID: {ToolState.RETRY_REQUIRED},
    ToolState.RETRY_REQUIRED: {ToolState.TOOL_REQUESTED},
    ToolState.VALID: {ToolState.EXECUTING},
    ToolState.EXECUTING: {ToolState.SUCCEEDED, ToolState.FAILED},
    ToolState.FAILED: {ToolState.RETRY_REQUIRED},
    ToolState.SUCCEEDED: set(),
}


@dataclass(slots=True)
class ToolAttempt:
    tool: str
    arguments: dict
    state: ToolState = ToolState.TOOL_REQUESTED
    error_category: str | None = None
    error_detail: str | None = None
    attempts: int = 1


@dataclass(slots=True)
class GroundingPolicy:
    max_tool_retries: int = 2
    require_successful_data: bool = True
    history: list[ToolAttempt] = field(default_factory=list)

    def record_invalid(self, tool: str, arguments: dict, detail: str) -> ToolAttempt:
        attempt = ToolAttempt(tool, arguments, ToolState.INVALID, "VALIDATION_ERROR", detail)
        self.history.append(attempt)
        return attempt

    def record_failure(self, tool: str, arguments: dict, category: str, detail: str) -> ToolAttempt:
        attempt = ToolAttempt(tool, arguments, ToolState.FAILED, category, detail)
        self.history.append(attempt)
        return attempt

    def record_success(self, tool: str, arguments: dict) -> ToolAttempt:
        attempt = ToolAttempt(tool, arguments, ToolState.SUCCEEDED)
        self.history.append(attempt)
        return attempt

    @property
    def has_successful_call(self) -> bool:
        return any(a.state == ToolState.SUCCEEDED for a in self.history)

    @property
    def has_failed_call(self) -> bool:
        return any(a.state in {ToolState.INVALID, ToolState.FAILED} for a in self.history)

    def retries_exhausted(self) -> bool:
        failures = [a for a in self.history if a.state in {ToolState.INVALID, ToolState.FAILED}]
        return len(failures) > self.max_tool_retries


SAFE_FAILURE_MESSAGE = (
    "I couldn't complete this request because the required data could not be "
    "retrieved. No verified result is available, so I won't guess."
)


def format_validation_error(field_name: str, received: object, allowed: str) -> str:
    return (
        f"VALIDATION_ERROR\n"
        f"Field: {field_name}\n"
        f"Received: {received}\n"
        f"Allowed: {allowed}\n\n"
        f"Retry the tool with valid arguments."
    )


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def classify_response(text: str, policy: GroundingPolicy) -> tuple[bool, str]:
    """Decide whether a proposed final answer may be sent."""
    if not policy.require_successful_data:
        return True, text
    if policy.has_successful_call:
        return True, text
    if not policy.has_failed_call:
        return True, text
    if policy.retries_exhausted() or _NUMBER.search(text or ""):
        reason = (
            " The agent repeatedly generated invalid tool arguments."
            if policy.retries_exhausted()
            else " The tool calls did not succeed."
        )
        return False, SAFE_FAILURE_MESSAGE + reason
    return True, text
