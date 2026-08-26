"""Athena agent API: run creation, activity feed (SSE), approvals."""

from __future__ import annotations

import asyncio
import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.agent import runner
from app.api.schemas import AgentRunRequest, ApprovalDecisionRequest
from app.core.auth.api import DbSession, current_user
from app.core.database.domain_models import (
    AgentRun,
    AgentStep,
    ApprovalRequest,
    ApprovalStatus,
    RunStatus,
)
from app.core.observability.metrics import inc

router = APIRouter(tags=["agent"])


def _org_id(user) -> int | None:
    return getattr(user, "organization_id", None)


def _run_dict(run: AgentRun) -> dict:
    return {
        "id": run.id,
        "status": run.status,
        "request": run.user_request,
        "final_response": run.final_response,
        "error": run.error,
        "iterations": run.iterations,
        "tool_calls_made": run.tool_calls_made,
        "started_at": str(run.started_at),
        "completed_at": str(run.completed_at) if run.completed_at else None,
    }


@router.post("/agent/run", status_code=202)
def start_run(
    payload: AgentRunRequest,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    inc("agent_runs_total")
    run = runner.create_run(
        db,
        payload.message,
        session_id=payload.session_id,
        organization_id=_org_id(user),
        project_id=payload.project_id,
    )
    runner.submit_run(run.id)
    return {"run_id": run.id, "status": run.status}


@router.get("/agent/runs")
def list_runs(
    limit: int = 20,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    query = select(AgentRun).order_by(AgentRun.started_at.desc()).limit(min(limit, 100))
    org = _org_id(user)
    if org is not None:
        query = query.where(AgentRun.organization_id == org)
    rows = db.execute(query).scalars().all()
    return {"count": len(rows), "runs": [_run_dict(r) for r in rows]}


@router.get("/agent/runs/{run_id}")
def get_run(run_id: int, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    run = _get_scoped_run(db, run_id, _org_id(user))
    steps = db.execute(
        select(AgentStep).where(AgentStep.agent_run_id == run.id).order_by(AgentStep.step_number)
    ).scalars().all()
    result = _run_dict(run)
    result["steps"] = [
        {
            "step_number": s.step_number,
            "type": s.step_type,
            "message": s.message,
            "tool": s.tool_name,
            "status": s.status,
        }
        for s in steps
    ]
    return result


def _get_scoped_run(db, run_id: int, org: int | None) -> AgentRun:
    run = db.get(AgentRun, run_id)
    if run is None or (org is not None and run.organization_id != org):
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@router.get("/agent/runs/{run_id}/events")
async def run_events(run_id: int, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None):
    """SSE stream of an agent run's step feed until it reaches a terminal state."""
    org = _org_id(user)
    last_step = 0

    async def event_stream():
        nonlocal last_step
        deadline_ticks = 600  # ~5 minutes at 0.5s cadence
        while deadline_ticks > 0:
            row = db.get(AgentRun, run_id)
            if row is None or (org is not None and row.organization_id != org):
                yield f"event: error\ndata: {json.dumps({'detail': 'Run not found'})}\n\n"
                return
            steps = db.execute(
                select(AgentStep)
                .where(AgentStep.agent_run_id == run_id, AgentStep.step_number > last_step)
                .order_by(AgentStep.step_number)
            ).scalars().all()
            for step in steps:
                last_step = step.step_number
                payload = json.dumps({
                    "step_number": step.step_number,
                    "type": step.step_type,
                    "message": step.message,
                    "tool": step.tool_name,
                    "status": step.status,
                })
                yield f"data: {payload}\n\n"
            if row.status in RunStatus.terminal():
                yield f"event: done\ndata: {json.dumps({'status': row.status})}\n\n"
                return
            deadline_ticks -= 1
            await asyncio.sleep(0.5)
        yield f"event: timeout\ndata: {json.dumps({'detail': 'event stream timed out'})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --- approvals -----------------------------------------------------------------
@router.get("/approvals")
def list_approvals(
    status_filter: str | None = None,
    limit: int = 50,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    query = (
        select(ApprovalRequest)
        .order_by(ApprovalRequest.created_at.desc())
        .limit(min(limit, 100))
    )
    if status_filter:
        query = query.where(ApprovalRequest.status == status_filter.upper())
    else:
        query = query.where(ApprovalRequest.status == ApprovalStatus.PENDING)
    org = _org_id(user)
    if org is not None:
        query = query.where(ApprovalRequest.organization_id == org)
    rows = db.execute(query).scalars().all()
    return {
        "count": len(rows),
        "approvals": [
            {
                "id": r.id,
                "run_id": r.agent_run_id,
                "tool": r.tool_name,
                "summary": r.summary,
                "preview": r.preview,
                "status": r.status,
                "created_at": str(r.created_at),
            }
            for r in rows
        ],
    }


@router.post("/approvals/{approval_id}/resolve")
def resolve_approval(
    approval_id: int,
    payload: ApprovalDecisionRequest,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    approval = db.get(ApprovalRequest, approval_id)
    org = _org_id(user)
    if approval is None or (org is not None and approval.organization_id != org):
        raise HTTPException(status_code=404, detail="Approval not found")
    if approval.status != ApprovalStatus.PENDING:
        raise HTTPException(status_code=409, detail="Approval already resolved")

    approval.status = ApprovalStatus.APPROVED if payload.approved else ApprovalStatus.REJECTED
    approval.decision_note = payload.note
    approval.resolved_at = __import__("app.core.database.models", fromlist=["utcnow"]).utcnow()
    db.commit()

    # Resume the paused run so the engine can execute (or skip) the write.
    runner.submit_resume(approval.agent_run_id)
    return {"id": approval.id, "status": approval.status}


__all__ = ["router"]
