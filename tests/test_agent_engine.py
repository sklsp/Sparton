"""Tests for app/agent/engine.py -- the Athena execution loop.

The engine is driven with a scripted DeterministicProvider (no network): each
script entry is either a final answer string or {"tool": name, "arguments": ...}.
Every test asserts on the AgentRun row and the AgentStep audit trail it writes.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent.engine import AgentEngine
from app.agent.registry import ToolAccess, ToolRegistry
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
from app.llm import DeterministicProvider, LLMError

# --------------------------------------------------------------------------
# Fakes: a small registry of demo tools and scripted providers
# --------------------------------------------------------------------------

class OkInput(BaseModel):
    limit: int = Field(default=5, ge=1, le=10)


class WriteInput(BaseModel):
    sku: str
    price: float


def _registry() -> ToolRegistry:
    reg = ToolRegistry()

    @reg.tool(name="demo.ok", description="Read demo rows.", category="test",
              access=ToolAccess.READ, input_model=OkInput)
    def ok(ctx, params):
        return {"count": 2, "items": [{"id": 1}, {"id": 2}]}

    @reg.tool(name="demo.boom", description="Always fails.", category="test",
              access=ToolAccess.READ, input_model=OkInput)
    def boom(ctx, params):
        raise RuntimeError("kaboom")

    @reg.tool(name="demo.big", description="Returns a huge payload.", category="test",
              access=ToolAccess.READ, input_model=OkInput)
    def big(ctx, params):
        return {"blob": "x" * 30_000}

    @reg.tool(name="demo.text", description="Returns plain text.", category="test",
              access=ToolAccess.READ, input_model=OkInput)
    def text_tool(ctx, params):
        return "hello"

    @reg.tool(name="demo.search", description="Searches documents.", category="test",
              access=ToolAccess.READ, input_model=OkInput)
    def search(ctx, params):
        return {"chunks": 3}

    @reg.tool(name="demo.investigate", description="Finds opportunities.", category="test",
              access=ToolAccess.READ, input_model=OkInput)
    def investigate(ctx, params):
        return {"opportunities_found": 4}

    @reg.tool(name="demo.write", description="Update a price (needs approval).",
              category="test", access=ToolAccess.WRITE, input_model=WriteInput)
    def write(ctx, params):
        return {"updated_fields": ["price"], "sku": params.sku}

    return reg


class _RaisingProvider(DeterministicProvider):
    """chat() always raises the given exception."""

    def __init__(self, exc: Exception) -> None:
        super().__init__()
        self._exc = exc

    def chat(self, messages, tools=None, **kwargs):
        raise self._exc


class _WrapUpFailsProvider(DeterministicProvider):
    """First chat returns a tool call; the wrap-up chat (tools=None) raises."""

    def __init__(self) -> None:
        super().__init__([{"tool": "demo.ok", "arguments": {}}])

    def chat(self, messages, tools=None, **kwargs):
        if tools is None:
            raise LLMError("summariser down")
        return super().chat(messages, tools, **kwargs)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _make_run(db_session, request: str = "Do the thing") -> AgentRun:
    run = AgentRun(organization_id=1, user_request=request)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _engine(db_session, llm, registry=None) -> AgentEngine:
    return AgentEngine(db=db_session, llm=llm, registry=registry or _registry())


def _steps(db_session, run_id: int) -> list[AgentStep]:
    rows = db_session.execute(
        select(AgentStep)
        .where(AgentStep.agent_run_id == run_id)
        .order_by(AgentStep.step_number)
    ).scalars().all()
    return list(rows)


def _steps_of_type(steps: list[AgentStep], step_type: str) -> list[AgentStep]:
    return [s for s in steps if s.step_type == step_type]


# --------------------------------------------------------------------------
# Completion paths
# --------------------------------------------------------------------------

def test_run_finishes_with_final_answer(db_session):
    provider = DeterministicProvider(["All done."])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    assert result.final_response == "All done."
    assert result.completed_at is not None
    assert result.pending_tool_calls == []
    # start() seeds the conversation with system + user messages.
    assert [m["role"] for m in run.messages] == ["system", "user"]

    steps = _steps(db_session, run.id)
    assert [s.step_type for s in steps] == [StepType.REQUEST, StepType.FINAL]
    final = steps[-1]
    assert final.output == {"response": "All done."}


def test_successful_tool_call_is_recorded_and_summarized(db_session):
    provider = DeterministicProvider([
        {"tool": "demo.ok", "arguments": {"limit": 3}},
        "Verified.",
    ])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    assert result.final_response == "Verified."
    assert result.tool_calls_made == 1
    assert result.iterations == 2

    steps = _steps(db_session, run.id)
    types = [s.step_type for s in steps]
    assert types == [
        StepType.REQUEST, StepType.DECISION, StepType.TOOL_CALL,
        StepType.TOOL_RESULT, StepType.FINAL,
    ]
    decision = _steps_of_type(steps, StepType.DECISION)[0]
    assert "demo.ok" in decision.message

    tool_call = _steps_of_type(steps, StepType.TOOL_CALL)[0]
    assert tool_call.tool_name == "demo.ok"
    assert tool_call.input == {"limit": 3}

    tool_result = _steps_of_type(steps, StepType.TOOL_RESULT)[0]
    assert tool_result.status == StepStatus.OK
    assert tool_result.output["count"] == 2
    # count + items shape gets the "Retrieved N rows" summary line.
    assert tool_result.message.startswith("Retrieved 2 rows")

    # The tool result is appended to the stored conversation as a tool message.
    last = run.messages[-1]
    assert last["role"] == "tool"
    assert json.loads(last["content"])["count"] == 2


@pytest.mark.parametrize(
    ("tool", "expected_prefix"),
    [
        ("demo.text", "demo.text returned a result"),
        ("demo.search", "Searched documents: 3 matching chunks"),
        ("demo.investigate", "Investigation found 4 opportunities"),
    ],
)
def test_summarize_variants(db_session, tool, expected_prefix):
    provider = DeterministicProvider([{"tool": tool, "arguments": {}}, "Done."])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    tool_result = _steps_of_type(_steps(db_session, run.id), StepType.TOOL_RESULT)[0]
    assert tool_result.message.startswith(expected_prefix)


def test_empty_final_answer_uses_placeholder(db_session):
    provider = DeterministicProvider(["   "])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    assert result.final_response == "The agent finished without producing an answer."


# --------------------------------------------------------------------------
# Tool failure paths
# --------------------------------------------------------------------------

def test_tool_error_yields_retryable_result_and_run_recovers(db_session):
    provider = DeterministicProvider([
        {"tool": "demo.boom", "arguments": {}},
        "Could not verify.",
    ])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    # One failure with retries left and no numbers in the answer: allowed.
    assert result.status == RunStatus.COMPLETED
    assert result.final_response == "Could not verify."

    tool_result = _steps_of_type(_steps(db_session, run.id), StepType.TOOL_RESULT)[0]
    assert tool_result.status == StepStatus.ERROR
    assert tool_result.output["category"] == "INTERNAL_ERROR"
    assert tool_result.output["retryable"] is True
    assert "kaboom" in tool_result.output["error"]


def test_validation_error_includes_correction(db_session):
    provider = DeterministicProvider([
        {"tool": "demo.ok", "arguments": {"limit": 99}},
        {"tool": "demo.ok", "arguments": {}},
        "Done.",
    ])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    assert result.tool_calls_made == 2

    tool_results = _steps_of_type(_steps(db_session, run.id), StepType.TOOL_RESULT)
    bad = tool_results[0]
    assert bad.status == StepStatus.ERROR
    assert bad.output["category"] == "VALIDATION_ERROR"
    assert bad.output["retryable"] is True
    correction = bad.output["correction"]
    assert "VALIDATION_ERROR" in correction
    assert "Field: limit" in correction
    assert "Allowed: <= 10" in correction
    assert "Retry the tool with valid arguments." in correction

    good = tool_results[1]
    assert good.status == StepStatus.OK


def test_type_error_correction_has_no_range(db_session):
    provider = DeterministicProvider([
        {"tool": "demo.ok", "arguments": {"limit": "abc"}},
        {"tool": "demo.ok", "arguments": {}},
        "Done.",
    ])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    bad = _steps_of_type(_steps(db_session, run.id), StepType.TOOL_RESULT)[0]
    assert bad.output["category"] == "VALIDATION_ERROR"
    # No numeric bounds in the error detail -> generic range hint.
    assert "Allowed: see the tool schema" in bad.output["correction"]


def test_unknown_tool_reported_not_found(db_session):
    provider = DeterministicProvider([
        {"tool": "demo.missing", "arguments": {}},
        "Done.",
    ])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    tool_result = _steps_of_type(_steps(db_session, run.id), StepType.TOOL_RESULT)[0]
    assert tool_result.status == StepStatus.ERROR
    assert tool_result.output["category"] == "NOT_FOUND"
    assert tool_result.output["retryable"] is False


def test_ungrounded_answer_is_rejected_then_tool_recovers(db_session):
    provider = DeterministicProvider([
        {"tool": "demo.boom", "arguments": {}},
        "There are 5 items.",
        {"tool": "demo.ok", "arguments": {}},
        "Verified.",
    ])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    assert result.final_response == "Verified."
    assert result.tool_calls_made == 2

    # The engine pushed a corrective user message back into the conversation.
    corrective = [m for m in run.messages if m.get("content") and "relied on data" in m["content"]]
    assert len(corrective) == 1


def test_retries_exhausted_fails_run(db_session):
    provider = DeterministicProvider([
        {"tool": "demo.ok", "arguments": {"limit": 99}},
        {"tool": "demo.ok", "arguments": {"limit": 99}},
        {"tool": "demo.ok", "arguments": {"limit": 99}},
    ])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.FAILED
    assert "repeatedly generated invalid tool arguments" in result.error
    assert result.final_response.startswith("The run could not be completed:")
    # The loop failed before a fourth LLM call.
    assert len(provider.calls) == 3
    last = _steps(db_session, run.id)[-1]
    assert last.step_type == StepType.ERROR


# --------------------------------------------------------------------------
# Limits and wrap-up
# --------------------------------------------------------------------------

def test_iteration_limit_triggers_wrap_up(db_session, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_iterations", 1)
    provider = DeterministicProvider([
        {"tool": "demo.ok", "arguments": {}},
        "Summary of what I found.",
    ])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    assert "Stopped early: iteration limit reached" in result.final_response

    decision = [s for s in _steps_of_type(_steps(db_session, run.id), StepType.DECISION)
                if s.message.startswith("Stopping early")]
    assert len(decision) == 1
    assert decision[0].status == StepStatus.ERROR


def test_tool_call_limit_triggers_wrap_up(db_session, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_tool_calls", 2)
    provider = DeterministicProvider([
        {"tool": "demo.ok", "arguments": {}},
        {"tool": "demo.ok", "arguments": {}},
        "Wrapped.",
    ])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    assert result.tool_calls_made == 2
    assert "Stopped early: tool call limit reached" in result.final_response


def test_wrap_up_with_ungrounded_summary_fails(db_session, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_iterations", 1)
    provider = DeterministicProvider([
        {"tool": "demo.boom", "arguments": {}},
        "There are 5 items.",
    ])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    # The wrap-up answer states a number without any successful data: blocked.
    assert result.status == RunStatus.FAILED
    assert "No verified result is available" in result.final_response


def test_wrap_up_llm_error_still_completes(db_session, monkeypatch):
    monkeypatch.setattr(settings, "agent_max_iterations", 1)
    provider = _WrapUpFailsProvider()
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    assert "could not summarise (summariser down)" in result.final_response
    assert "(Stopped early: iteration limit reached)" in result.final_response


# --------------------------------------------------------------------------
# Approvals
# --------------------------------------------------------------------------

def _approval(db_session, run_id: int) -> ApprovalRequest:
    return db_session.execute(
        select(ApprovalRequest).where(ApprovalRequest.agent_run_id == run_id)
    ).scalars().one()


def test_approval_pause_approve_and_resume(db_session):
    provider = DeterministicProvider([
        {"tool": "demo.write", "arguments": {"sku": "SKU-1", "price": 9.9}},
        "Updated.",
    ])
    engine = _engine(db_session, provider)
    run = _make_run(db_session)

    paused = engine.start(run)
    assert paused.status == RunStatus.WAITING_FOR_APPROVAL

    approval = _approval(db_session, run.id)
    assert approval.status == ApprovalStatus.PENDING
    assert approval.tool_name == "demo.write"
    assert approval.payload == {"sku": "SKU-1", "price": 9.9}
    # The queued call now carries the approval id so resume can find it.
    assert run.pending_tool_calls[0]["approval_id"] == approval.id

    steps = _steps(db_session, run.id)
    request_step = _steps_of_type(steps, StepType.APPROVAL_REQUEST)[0]
    assert request_step.status == StepStatus.PENDING
    # The write tool must not have executed while waiting.
    assert _steps_of_type(steps, StepType.TOOL_CALL) == []

    approval.status = ApprovalStatus.APPROVED
    approval.decision_note = "ok"
    approval.resolved_at = utcnow()
    db_session.commit()

    finished = engine.resume(run)
    assert finished.status == RunStatus.COMPLETED
    assert finished.final_response == "Updated."
    assert finished.tool_calls_made == 1

    # Executing the approved call stores its result on the approval row.
    db_session.refresh(approval)
    assert approval.result == {"updated_fields": ["price"], "sku": "SKU-1"}

    steps = _steps(db_session, run.id)
    resolved = _steps_of_type(steps, StepType.APPROVAL_RESOLVED)[0]
    assert resolved.message == "Approval granted for demo.write"
    tool_call = _steps_of_type(steps, StepType.TOOL_CALL)[0]
    assert tool_call.tool_name == "demo.write"


def test_approval_rejection_reports_not_applied(db_session):
    provider = DeterministicProvider([
        {"tool": "demo.write", "arguments": {"sku": "SKU-2", "price": 5.0}},
        "Not applied.",
    ])
    engine = _engine(db_session, provider)
    run = _make_run(db_session)

    paused = engine.start(run)
    assert paused.status == RunStatus.WAITING_FOR_APPROVAL

    approval = _approval(db_session, run.id)
    approval.status = ApprovalStatus.REJECTED
    approval.decision_note = "too expensive"
    approval.resolved_at = utcnow()
    db_session.commit()

    finished = engine.resume(run)
    assert finished.status == RunStatus.COMPLETED
    assert finished.final_response == "Not applied."
    # The rejected tool never executed.
    assert finished.tool_calls_made == 0
    db_session.refresh(approval)
    assert approval.result is None

    steps = _steps(db_session, run.id)
    resolved = _steps_of_type(steps, StepType.APPROVAL_RESOLVED)[0]
    assert resolved.status == StepStatus.ERROR
    tool_result = _steps_of_type(steps, StepType.TOOL_RESULT)[0]
    assert tool_result.output["executed"] is False
    assert tool_result.output["note"] == "too expensive"
    assert _steps_of_type(steps, StepType.TOOL_CALL) == []


def test_resume_while_approval_still_pending_keeps_waiting(db_session):
    provider = DeterministicProvider([
        {"tool": "demo.write", "arguments": {"sku": "SKU-3", "price": 1.0}},
        "Done.",
    ])
    engine = _engine(db_session, provider)
    run = _make_run(db_session)

    paused = engine.start(run)
    assert paused.status == RunStatus.WAITING_FOR_APPROVAL

    # No human decision yet: resuming must not execute the tool or duplicate
    # the approval request.
    again = engine.resume(run)
    assert again.status == RunStatus.WAITING_FOR_APPROVAL
    approvals = db_session.execute(
        select(ApprovalRequest).where(ApprovalRequest.agent_run_id == run.id)
    ).scalars().all()
    assert len(approvals) == 1
    steps = _steps(db_session, run.id)
    assert _steps_of_type(steps, StepType.TOOL_CALL) == []


# --------------------------------------------------------------------------
# Engine-level failures
# --------------------------------------------------------------------------

def test_llm_error_fails_run(db_session):
    provider = _RaisingProvider(LLMError("upstream down"))
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.FAILED
    assert result.error == "upstream down"
    assert result.final_response.startswith("The run could not be completed: upstream down")


def test_unexpected_exception_fails_run(db_session):
    provider = _RaisingProvider(ValueError("weird"))
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.FAILED
    assert result.error == "Unexpected error: weird"


def test_timeout_fails_run(db_session, monkeypatch):
    monkeypatch.setattr(settings, "agent_timeout_seconds", -10)
    provider = DeterministicProvider(["Never."])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.FAILED
    assert result.error == "The agent exceeded its time budget"
    # The deadline expired before any LLM call.
    assert provider.calls == []


def test_large_tool_output_is_truncated(db_session):
    provider = DeterministicProvider([{"tool": "demo.big", "arguments": {}}, "Done."])
    run = _make_run(db_session)
    result = _engine(db_session, provider).start(run)

    assert result.status == RunStatus.COMPLETED
    tool_result = _steps_of_type(_steps(db_session, run.id), StepType.TOOL_RESULT)[0]
    assert tool_result.output["truncated"] is True
    assert len(tool_result.output["preview"]) == 20_000





