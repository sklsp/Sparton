"""The Athena agent execution loop.

Adapted from Ares `app/agent/engine.py`:

    understand -> decide -> call tools -> inspect results -> decide again
                                       -> pause for approval on writes
                                       -> final answer

The loop is explicit and bounded (iterations, tool calls, wall clock). All
state that survives a pause lives on the ``AgentRun`` row, so a run waiting
for human approval can be resumed by any worker. Domain coupling was removed:
tools receive a generic ToolContext (db + llm + tenant scope + services).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.grounding import (
    GroundingPolicy,
    classify_response,
    format_validation_error,
)
from app.agent.prompts import system_prompt
from app.agent.registry import ToolContext, ToolError, ToolRegistry, ToolValidationError
from app.core.config import settings
from app.core.database.domain_models import (
    AgentRun,
    AgentStep,
    ApprovalRequest,
    ApprovalStatus,
    RunStatus,
    StepStatus,
    StepType,
    utcnow,
)
from app.llm import LLMError, LLMProvider, Message

logger = logging.getLogger(__name__)

MAX_STORED_OUTPUT_CHARS = 20_000


class AgentTimeout(RuntimeError):
    pass


def _first_field(detail: str) -> str:
    match = re.search(r"-\s*([a-z_]+)\s*:", detail)
    if match:
        return match.group(1)
    match = re.search(r"'([a-z_]+)'", detail)
    return match.group(1) if match else "arguments"


def _received_value(detail: str) -> str:
    numbers = re.findall(r"\d+(?:\.\d+)?", detail)
    return numbers[0] if numbers else "(unparseable)"


def _allowed_range(detail: str) -> str:
    bounds = re.findall(
        r"(?:less than or equal to|greater than or equal to|<=|>=)\s*(\d+)", detail
    )
    if len(bounds) >= 2:
        return f"{bounds[1]}-{bounds[0]}"
    if len(bounds) == 1:
        return f"<= {bounds[0]}"
    return "see the tool schema"


@dataclass(slots=True)
class _Deadline:
    expires_at: float

    def check(self) -> None:
        if time.monotonic() > self.expires_at:
            raise AgentTimeout("The agent exceeded its time budget")


class AgentEngine:
    """Drives one run at a time against one database session."""

    def __init__(
        self,
        db: Session,
        llm: LLMProvider,
        registry: ToolRegistry,
        ctx: ToolContext | None = None,
    ) -> None:
        self.db = db
        self.llm = llm
        self.registry = registry
        self.ctx = ctx or ToolContext(db=db, llm=llm)
        self.grounding = GroundingPolicy(
            max_tool_retries=settings.max_tool_retries,
            require_successful_data=settings.strict_tool_grounding,
        )

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------
    def start(self, run: AgentRun) -> AgentRun:
        logger.info("Agent run started: id=%s request=%r", run.id, run.user_request[:80])
        messages = [
            Message(role="system", content=system_prompt()),
            Message(role="user", content=run.user_request),
        ]
        self._save_messages(run, messages)
        self._record(run, StepType.REQUEST, "Analyzing request")
        return self._loop(run)

    def resume(self, run: AgentRun) -> AgentRun:
        logger.info("Agent run resumed: id=%s", run.id)
        run.status = RunStatus.RUNNING.value
        self.db.commit()
        return self._loop(run)

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    def _loop(self, run: AgentRun) -> AgentRun:
        deadline = _Deadline(time.monotonic() + settings.agent_timeout_seconds)
        try:
            while True:
                deadline.check()

                if run.pending_tool_calls and self._drain_pending(run, deadline):
                    run.status = RunStatus.WAITING_FOR_APPROVAL.value
                    self.db.commit()
                    return run

                if run.iterations >= settings.agent_max_iterations:
                    return self._wrap_up(run, "iteration limit reached")
                if run.tool_calls_made >= settings.agent_max_tool_calls:
                    return self._wrap_up(run, "tool call limit reached")
                if self.grounding.retries_exhausted():
                    reason = (
                        "the agent repeatedly generated invalid tool arguments"
                        if any(a.error_category == "VALIDATION_ERROR" for a in self.grounding.history)
                        else "repeated tool failures"
                    )
                    return self._fail(
                        run,
                        f"I couldn't complete the requested operation because {reason}. "
                        f"No unverified result was returned.",
                    )

                messages = self._load_messages(run)
                response = self.llm.chat(messages, self.registry.llm_specs())
                run.iterations += 1

                if not response.tool_calls:
                    allowed, text = classify_response(response.content or "", self.grounding)
                    if not allowed:
                        if self.grounding.retries_exhausted():
                            logger.warning("Run %s: blocked ungrounded answer; retries exhausted", run.id)
                            self._record(
                                run,
                                StepType.ERROR,
                                "Blocked an unverified answer: required tool data was never retrieved",
                            )
                            return self._fail(run, text)
                        logger.info("Run %s: rejected ungrounded answer; returning to tool loop", run.id)
                        messages.append(Message(
                            role="user",
                            content=(
                                "Your previous answer relied on data that was never "
                                "retrieved: the tool calls failed. Do not state facts. "
                                "Call the tool again with valid arguments, or say you "
                                "could not verify."
                            ),
                        ))
                        self._save_messages(run, messages)
                        continue
                    return self._complete(run, response.content)

                names = ", ".join(tc.name for tc in response.tool_calls)
                self._record(run, StepType.DECISION, f"Decided to call: {names}")
                messages.append(Message(role="assistant", content=response.content, tool_calls=response.tool_calls))
                self._save_messages(run, messages)
                run.pending_tool_calls = [tc.to_dict() for tc in response.tool_calls]
                self.db.commit()

        except AgentTimeout as exc:
            return self._fail(run, str(exc))
        except LLMError as exc:
            logger.warning("Run %s failed on LLM error: %s", run.id, exc)
            return self._fail(run, str(exc))
        except Exception as exc:  # noqa: BLE001 - never let a run kill the API
            logger.exception("Run %s crashed", run.id)
            return self._fail(run, f"Unexpected error: {exc}")

    # ------------------------------------------------------------------
    # Tool call handling
    # ------------------------------------------------------------------
    def _drain_pending(self, run: AgentRun, deadline: _Deadline) -> bool:
        """Execute queued tool calls. Returns True if the run paused for approval."""
        while run.pending_tool_calls:
            deadline.check()
            raw = dict(run.pending_tool_calls[0])
            call = Message.from_dict({"role": "assistant", "tool_calls": [raw]}).tool_calls[0]

            if not self.registry.has(call.name):
                self.grounding.record_failure(call.name, call.arguments, "NOT_FOUND", f"Unknown tool {call.name}")
                self._finish_call(
                    run, call,
                    {"error": f"Unknown tool '{call.name}'", "category": "NOT_FOUND", "retryable": False},
                    status=StepStatus.ERROR,
                    message=f"Unknown tool: {call.name}",
                )
                continue

            tool = self.registry.get(call.name)
            if tool.requires_approval:
                decision = self._approval_state(run, raw, call)
                if decision is None:
                    return True  # waiting for a human
                if decision.status == ApprovalStatus.REJECTED.value:
                    self._record(
                        run, StepType.APPROVAL_RESOLVED,
                        f"Approval rejected for {call.name}",
                        tool_name=call.name, status=StepStatus.ERROR,
                    )
                    self._finish_call(
                        run, call,
                        {
                            "executed": False,
                            "reason": "A human rejected this change. Do not retry it. "
                                      "Report it as not applied.",
                            "note": decision.decision_note,
                        },
                        status=StepStatus.ERROR,
                        message=f"{call.name} was rejected, nothing was changed",
                    )
                    continue

                self._record(
                    run, StepType.APPROVAL_RESOLVED,
                    f"Approval granted for {call.name}", tool_name=call.name,
                )

            result, status, message = self._execute(run, call)
            if status is StepStatus.OK and tool.requires_approval:
                self._store_approval_result(run, raw, result)
            self._finish_call(run, call, result, status=status, message=message)

        return False

    def _execute(self, run: AgentRun, call) -> tuple[Any, StepStatus, str]:
        self._record(
            run, StepType.TOOL_CALL, f"Calling {call.name}",
            tool_name=call.name, input=call.arguments,
        )
        started = time.monotonic()
        run.tool_calls_made += 1
        try:
            result = self.registry.execute(call.name, call.arguments, self.ctx)
        except ToolValidationError as exc:
            detail = str(exc)
            self.grounding.record_invalid(call.name, call.arguments, detail)
            correction = format_validation_error(
                _first_field(detail), _received_value(detail), _allowed_range(detail),
            )
            logger.warning("Tool %s rejected arguments: %s", call.name, detail)
            return (
                {"error": detail, "correction": correction,
                 "category": "VALIDATION_ERROR", "retryable": True},
                StepStatus.ERROR,
                f"{call.name} arguments invalid: {detail}",
            )
        except ToolError as exc:
            self.grounding.record_failure(call.name, call.arguments, "INTERNAL_ERROR", str(exc))
            logger.warning("Tool %s failed: %s", call.name, exc)
            return (
                {"error": str(exc), "category": "INTERNAL_ERROR", "retryable": True},
                StepStatus.ERROR,
                f"{call.name} failed: {exc}",
            )
        else:
            self.grounding.record_success(call.name, call.arguments)
            duration = int((time.monotonic() - started) * 1000)
            return result, StepStatus.OK, _summarize(call.name, result, duration)

    def _finish_call(self, run: AgentRun, call, result: Any, *, status: StepStatus, message: str) -> None:
        self._record(
            run, StepType.TOOL_RESULT, message,
            tool_name=call.name, output=result, status=status,
        )
        messages = self._load_messages(run)
        messages.append(Message.tool_result(call, result))
        self._save_messages(run, messages)
        run.pending_tool_calls = list(run.pending_tool_calls[1:])
        self.db.commit()

    # ------------------------------------------------------------------
    # Approvals
    # ------------------------------------------------------------------
    def _approval_state(self, run: AgentRun, raw: dict[str, Any], call):
        approval_id = raw.get("approval_id")
        if approval_id is not None:
            approval = self.db.get(ApprovalRequest, approval_id)
            if approval is None or approval.status == ApprovalStatus.PENDING.value:
                return None
            return approval

        approval = self._request_approval(run, call)
        raw["approval_id"] = approval.id
        run.pending_tool_calls = [raw, *list(run.pending_tool_calls[1:])]
        self.db.commit()
        logger.info("Approval requested: run=%s tool=%s id=%s", run.id, call.name, approval.id)
        return None

    def _request_approval(self, run: AgentRun, call) -> ApprovalRequest:
        preview = {"arguments": call.arguments}
        summary = f"Run {call.name}"
        approval = ApprovalRequest(
            organization_id=run.organization_id,
            agent_run_id=run.id,
            tool_name=call.name,
            payload=call.arguments,
            preview=preview,
            summary=summary,
        )
        self.db.add(approval)
        self.db.commit()
        self.db.refresh(approval)
        self._record(
            run, StepType.APPROVAL_REQUEST,
            f"Waiting for approval: {summary}",
            tool_name=call.name, input=call.arguments, status=StepStatus.PENDING,
        )
        return approval

    def _store_approval_result(self, run: AgentRun, raw: dict[str, Any], result: Any) -> None:
        approval = self.db.get(ApprovalRequest, raw.get("approval_id"))
        if approval is not None:
            approval.result = result
            self.db.commit()

    # ------------------------------------------------------------------
    # Termination
    # ------------------------------------------------------------------
    def _wrap_up(self, run: AgentRun, reason: str) -> AgentRun:
        logger.info("Run %s hit its %s", run.id, reason)
        self._record(
            run, StepType.DECISION,
            f"Stopping early ({reason}), summarising what was found",
            status=StepStatus.ERROR,
        )
        messages = self._load_messages(run)
        messages.append(Message(
            role="user",
            content=(
                "Stop calling tools now and answer with what you already know. "
                "Be explicit about anything you could not finish."
            ),
        ))
        try:
            response = self.llm.chat(messages, tools=None)
            allowed, text = classify_response(response.content or "", self.grounding)
            if not allowed:
                return self._fail(run, text)
        except LLMError as exc:
            text = f"The agent stopped after the {reason} and could not summarise ({exc})."
        return self._complete(run, text, note=f"Stopped early: {reason}")

    def _complete(self, run: AgentRun, text: str, note: str | None = None) -> AgentRun:
        final = (text or "").strip() or "The agent finished without producing an answer."
        if note:
            final = f"{final}\n\n({note})"
        run.final_response = final
        run.status = RunStatus.COMPLETED.value
        run.completed_at = utcnow()
        run.pending_tool_calls = []
        self._record(run, StepType.FINAL, "Run completed", output={"response": final})
        self.db.commit()
        logger.info("Agent run completed: id=%s", run.id)
        return run

    def _fail(self, run: AgentRun, error: str) -> AgentRun:
        run.status = RunStatus.FAILED.value
        run.error = error
        run.completed_at = utcnow()
        run.pending_tool_calls = []
        run.final_response = f"The run could not be completed: {error}"
        self._record(run, StepType.ERROR, error, status=StepStatus.ERROR)
        self.db.commit()
        logger.error("Agent run failed: id=%s error=%s", run.id, error)
        return run

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------
    def _load_messages(self, run: AgentRun) -> list[Message]:
        return [Message.from_dict(m) for m in run.messages or []]

    def _save_messages(self, run: AgentRun, messages: list[Message]) -> None:
        run.messages = [m.to_dict() for m in messages]

    def _next_step_number(self, run: AgentRun) -> int:
        current = self.db.execute(
            select(func.max(AgentStep.step_number)).where(AgentStep.agent_run_id == run.id)
        ).scalar()
        return (current or 0) + 1

    def _record(
        self,
        run: AgentRun,
        step_type: StepType,
        message: str,
        *,
        tool_name: str | None = None,
        input: dict[str, Any] | None = None,
        output: Any | None = None,
        status: StepStatus = StepStatus.OK,
        duration_ms: int | None = None,
    ) -> AgentStep:
        step = AgentStep(
            agent_run_id=run.id,
            step_number=self._next_step_number(run),
            step_type=step_type.value,
            message=message,
            tool_name=tool_name,
            input=input,
            output=_truncate(output),
            status=status.value,
            duration_ms=duration_ms,
        )
        self.db.add(step)
        self.db.commit()
        return step


def _truncate(output: Any) -> Any:
    if output is None:
        return None
    encoded = json.dumps(output, default=str)
    if len(encoded) <= MAX_STORED_OUTPUT_CHARS:
        return output
    return {"truncated": True, "preview": encoded[:MAX_STORED_OUTPUT_CHARS]}


def _summarize(tool_name: str, result: Any, duration_ms: int) -> str:
    """One operational line for the activity feed — no chain of thought."""
    if not isinstance(result, dict):
        return f"{tool_name} returned a result ({duration_ms} ms)"

    if "count" in result and ("products" in result or "items" in result):
        return f"Retrieved {result['count']} rows ({duration_ms} ms)"
    if "chunks" in result:
        return f"Searched documents: {result['chunks']} matching chunks ({duration_ms} ms)"
    if "updated_fields" in result:
        fields = ", ".join(result["updated_fields"]) or "nothing"
        return f"Updated {result.get('sku')}: {fields} ({duration_ms} ms)"
    if "opportunities_found" in result:
        return f"Investigation found {result['opportunities_found']} opportunities ({duration_ms} ms)"
    return f"{tool_name} completed ({duration_ms} ms)"
