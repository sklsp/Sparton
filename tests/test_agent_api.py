"""Athena agent API (app.api.agent_api): run creation, activity feed, approvals.

Runs execute inline (conftest sets AGENT_RUN_INLINE=true) against the scripted
DeterministicProvider, so POST /agent/run returns only after the whole agent
loop has finished and every assertion can read the final AgentRun row directly.
"""

from __future__ import annotations

import json

from sqlalchemy import select

import app.api.agent_api as agent_api
from app.core.database.domain_models import (
    AgentRun,
    ApprovalRequest,
    ApprovalStatus,
    RunStatus,
    StepType,
)
from app.core.database.identity import User

# Same admin email that conftest's auth_headers registers.
ADMIN_EMAIL = "admin@example.com"
FOREIGN_ORG = 999_999


def _org_id(db_session) -> int:
    user = db_session.execute(select(User).where(User.email == ADMIN_EMAIL)).scalars().first()
    assert user is not None and user.organization_id is not None
    return user.organization_id


def _start_run(client, headers, message="What can you do?") -> int:
    response = client.post("/agent/run", headers=headers, json={"message": message})
    assert response.status_code == 202, response.text
    body = response.json()
    # The row is created RUNNING; the inline worker finishes it before the
    # response goes out, but the handler reports the status at creation time.
    assert body["status"] == RunStatus.RUNNING
    return body["run_id"]


def _foreign_run(db_session) -> AgentRun:
    run = AgentRun(organization_id=FOREIGN_ORG, user_request="someone else's request")
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _terminal_run(db_session, org: int) -> AgentRun:
    """A finished run: resolve_approval's resume dispatch is a no-op on it."""
    run = AgentRun(organization_id=org, user_request="done", status=RunStatus.COMPLETED)
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _approval(db_session, run: AgentRun, org: int, status=ApprovalStatus.PENDING) -> ApprovalRequest:
    approval = ApprovalRequest(
        organization_id=org,
        agent_run_id=run.id,
        tool_name="write_note",
        payload={"text": "hello"},
        summary="Write a note",
        preview={"before": None, "after": "hello"},
        status=status,
    )
    db_session.add(approval)
    db_session.commit()
    db_session.refresh(approval)
    return approval


def _parse_sse(text: str) -> list[tuple[str | None, dict]]:
    """SSE blocks into (event, data) pairs; plain step frames have event=None."""
    events = []
    for block in text.split("\n\n"):
        event, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
        if event is not None or data is not None:
            events.append((event, data))
    return events


# --- POST /agent/run -----------------------------------------------------------


def test_start_run_returns_202_and_completes_inline(client, auth_headers):
    run_id = _start_run(client, auth_headers)

    response = client.get(f"/agent/runs/{run_id}", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == run_id
    assert body["status"] == RunStatus.COMPLETED
    assert body["final_response"] == "Deterministic response."
    assert body["error"] is None
    assert body["completed_at"] is not None
    assert body["iterations"] >= 1


def test_get_run_includes_the_step_audit_trail(client, auth_headers):
    run_id = _start_run(client, auth_headers)

    body = client.get(f"/agent/runs/{run_id}", headers=auth_headers).json()
    steps = body["steps"]
    assert len(steps) >= 2
    for step in steps:
        assert set(step) == {"step_number", "type", "message", "tool", "status"}
    assert [s["step_number"] for s in steps] == list(range(1, len(steps) + 1))
    assert steps[0]["type"] == StepType.REQUEST
    assert steps[-1]["type"] == StepType.FINAL


def test_start_run_rejects_bad_input(client, auth_headers):
    # message is required and must be non-empty (min_length=1).
    for payload in ({}, {"message": ""}):
        response = client.post("/agent/run", headers=auth_headers, json=payload)
        assert response.status_code == 422, response.text

    # session_id is capped at 64 characters.
    response = client.post(
        "/agent/run", headers=auth_headers, json={"message": "hi", "session_id": "x" * 65}
    )
    assert response.status_code == 422


def test_agent_routes_require_auth(client):
    for path in ("/agent/runs", "/approvals"):
        assert client.get(path).status_code == 401


# --- GET /agent/runs -----------------------------------------------------------


def test_list_runs_scopes_to_the_callers_org(client, auth_headers, db_session):
    first = _start_run(client, auth_headers)
    second = _start_run(client, auth_headers)
    foreign = _foreign_run(db_session)

    body = client.get("/agent/runs", headers=auth_headers).json()
    assert body["count"] == 2
    ids = [r["id"] for r in body["runs"]]
    # Newest first; the other tenant's run never appears.
    assert ids[0] == second
    assert set(ids) == {first, second}
    assert foreign.id not in ids

    entry = body["runs"][0]
    assert set(entry) == {
        "id", "status", "request", "final_response", "error",
        "iterations", "tool_calls_made", "started_at", "completed_at",
    }


def test_list_runs_honours_limit(client, auth_headers):
    _start_run(client, auth_headers)
    second = _start_run(client, auth_headers)

    body = client.get("/agent/runs?limit=1", headers=auth_headers).json()
    assert body["count"] == 1
    assert body["runs"][0]["id"] == second


def test_get_run_of_another_org_is_404(client, auth_headers, db_session):
    foreign = _foreign_run(db_session)

    response = client.get(f"/agent/runs/{foreign.id}", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "Run not found"


def test_get_missing_run_is_404(client, auth_headers):
    response = client.get("/agent/runs/999999", headers=auth_headers)
    assert response.status_code == 404


# --- GET /agent/runs/{run_id}/events (SSE) -------------------------------------


def test_events_stream_streams_steps_then_done(client, auth_headers):
    run_id = _start_run(client, auth_headers)

    response = client.get(f"/agent/runs/{run_id}/events", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(response.text)
    steps = [data for event, data in events if event is None]
    done = [(event, data) for event, data in events if event == "done"]
    assert len(steps) >= 2
    assert steps[0]["type"] == StepType.REQUEST
    assert done == [("done", {"status": RunStatus.COMPLETED})]


def test_events_stream_of_another_org_is_an_error_event(client, auth_headers, db_session):
    foreign = _foreign_run(db_session)

    response = client.get(f"/agent/runs/{foreign.id}/events", headers=auth_headers)
    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert events == [("error", {"detail": "Run not found"})]


def test_events_stream_times_out_for_a_run_that_never_finishes(
    client, auth_headers, db_session, monkeypatch
):
    run = AgentRun(organization_id=_org_id(db_session), user_request="stuck forever")
    db_session.add(run)
    db_session.commit()

    # The real stream polls every 0.5 s for up to ~5 minutes; make the ticks
    # instant so the deadline is exercised without waiting it out.
    class _InstantAsyncio:
        @staticmethod
        async def sleep(_delay):
            return None

    monkeypatch.setattr(agent_api, "asyncio", _InstantAsyncio)

    response = client.get(f"/agent/runs/{run.id}/events", headers=auth_headers)
    assert response.status_code == 200
    events = _parse_sse(response.text)
    assert events[-1] == ("timeout", {"detail": "event stream timed out"})


# --- approvals -----------------------------------------------------------------


def test_list_approvals_defaults_to_pending_and_scopes_org(client, auth_headers, db_session):
    org = _org_id(db_session)
    mine = _approval(db_session, _terminal_run(db_session, org), org)
    resolved = _approval(
        db_session, _terminal_run(db_session, org), org, status=ApprovalStatus.APPROVED
    )
    # A pending approval from another tenant must never appear, even though it
    # matches the default status filter.
    _approval(db_session, _terminal_run(db_session, FOREIGN_ORG), FOREIGN_ORG)

    body = client.get("/approvals", headers=auth_headers).json()
    assert body["count"] == 1
    entry = body["approvals"][0]
    assert entry["id"] == mine.id
    assert set(entry) == {"id", "run_id", "tool", "summary", "preview", "status", "created_at"}

    by_status = client.get("/approvals?status_filter=approved", headers=auth_headers).json()
    assert [a["id"] for a in by_status["approvals"]] == [resolved.id]


def test_resolve_approval_approves_and_records_the_decision(client, auth_headers, db_session):
    org = _org_id(db_session)
    approval = _approval(db_session, _terminal_run(db_session, org), org)

    response = client.post(
        f"/approvals/{approval.id}/resolve",
        headers=auth_headers,
        json={"approved": True, "note": "looks good"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"id": approval.id, "status": ApprovalStatus.APPROVED}

    db_session.refresh(approval)
    assert approval.decision_note == "looks good"
    assert approval.resolved_at is not None


def test_resolve_approval_rejects(client, auth_headers, db_session):
    org = _org_id(db_session)
    approval = _approval(db_session, _terminal_run(db_session, org), org)

    response = client.post(
        f"/approvals/{approval.id}/resolve", headers=auth_headers, json={"approved": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == ApprovalStatus.REJECTED


def test_resolve_approval_of_another_org_is_404(client, auth_headers, db_session):
    foreign = _approval(db_session, _terminal_run(db_session, FOREIGN_ORG), FOREIGN_ORG)

    response = client.post(
        f"/approvals/{foreign.id}/resolve", headers=auth_headers, json={"approved": True}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Approval not found"


def test_resolve_missing_approval_is_404(client, auth_headers):
    response = client.post(
        "/approvals/999999/resolve", headers=auth_headers, json={"approved": True}
    )
    assert response.status_code == 404


def test_resolving_an_already_resolved_approval_is_409(client, auth_headers, db_session):
    org = _org_id(db_session)
    approval = _approval(
        db_session, _terminal_run(db_session, org), org, status=ApprovalStatus.APPROVED
    )

    response = client.post(
        f"/approvals/{approval.id}/resolve", headers=auth_headers, json={"approved": False}
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Approval already resolved"
