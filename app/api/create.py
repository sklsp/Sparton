"""Create domain API: ComfyUI generation, datasets, LoRA training."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.schemas import (
    CaptionRequest,
    DatasetCreateRequest,
    GenerationRequest,
    TrainingProjectCreateRequest,
    TrainingStartRequest,
    WorkflowImportRequest,
)
from app.core.auth.api import DbSession, current_user
from app.core.config import settings
from app.core.database.creation_models import (
    DatasetImage,
    DatasetProject,
    GeneratedImage,
    TrainingProject,
)
from app.core.database.domain_models import Job
from app.core.jobs.queue import enqueue
from app.datasets.service import DatasetError, DatasetService
from app.generation.comfyui_client import ComfyUIError
from app.generation.service import ComfyUIService, WorkflowError
from app.llm import LLMError
from app.training.hardware import detect_hardware
from app.training.preflight import advise
from app.training.service import LoRATrainingService

router = APIRouter(tags=["create"])

_dataset_service: DatasetService | None = None


def get_dataset_service() -> DatasetService:
    global _dataset_service
    if _dataset_service is None:
        _dataset_service = DatasetService()
    return _dataset_service


def _get_comfyui():
    return ComfyUIService()


def _org_id(user) -> int | None:
    return getattr(user, "organization_id", None)


# --------------------------------------------------------------------------
# ComfyUI status + workflows
# --------------------------------------------------------------------------
@router.get("/comfyui/status")
def comfyui_status() -> dict:
    try:
        return _get_comfyui().status()
    except ComfyUIError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.get("/comfyui/workflows")
def list_workflows() -> dict:
    workflows = _get_comfyui().list_workflows()
    return {"count": len(workflows), "workflows": [w.to_dict() for w in workflows]}


@router.post("/comfyui/workflows", status_code=201)
def import_workflow(payload: WorkflowImportRequest) -> dict:
    try:
        info = _get_comfyui().save_workflow(
            payload.name, payload.graph,
            description=payload.description, arch=payload.arch, inputs=payload.inputs,
        )
        return info.to_dict()
    except WorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.delete("/comfyui/workflows/{workflow_id}")
def delete_workflow(workflow_id: str) -> dict:
    deleted = _get_comfyui().delete_workflow(workflow_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return {"deleted": True}


# --------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------
@router.post("/comfyui/validate-generation")
def validate_generation(payload: GenerationRequest) -> dict:
    params = {k: v for k, v in payload.model_dump().items() if v is not None}
    return _get_comfyui().validate_workflow_request(payload.workflow_id, params)


@router.post("/comfyui/generate", status_code=202)
def generate(
    payload: GenerationRequest,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    """Queue a synchronous-tracked generation job on the shared job system."""
    service = _get_comfyui()
    params = {k: v for k, v in payload.model_dump().items() if v is not None}
    try:
        graph, seed = service.build_graph(payload.workflow_id, params)
    except WorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    job, created = enqueue(
        db,
        type="comfy_generation",
        payload={
            "workflow_id": payload.workflow_id,
            "params": params,
            "seed": seed,
            "prompt": payload.prompt,
            "lora_name": payload.lora_name,
        },
        organization_id=_org_id(user),
        created_by=getattr(user, "id", None),
        idempotency_key=f"gen:{uuid.uuid4().hex[:12]}",
    )
    return {"job_id": job.id, "seed": seed, "status": job.status}


@router.get("/generated")
def list_generated(limit: int = 50, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    entries = _get_comfyui().list_generated(limit=min(limit, 200))
    org = _org_id(user)
    rows = db.execute(
        select(GeneratedImage).order_by(GeneratedImage.created_at.desc()).limit(min(limit, 200))
    ).scalars().all()
    by_filename = {r.filename: r.id for r in rows if org is None or r.organization_id == org}
    for entry in entries:
        entry["id"] = by_filename.get(entry["filename"])
        entry["url"] = f"/generated/{entry['filename']}"
    return {"count": len(entries), "images": entries}


@router.get("/generated/{filename}")
def read_generated(filename: str) -> FileResponse:
    service = _get_comfyui()
    try:
        content = service.read_generated(filename)
    except WorkflowError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.gettempdir()) / filename
    tmp.write_bytes(content)
    return FileResponse(tmp, media_type="image/png")


# --------------------------------------------------------------------------
# Datasets (Argo)
# --------------------------------------------------------------------------
@router.post("/datasets", status_code=201)
def create_dataset(payload: DatasetCreateRequest, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    row = get_dataset_service().create_dataset(
        db,
        name=payload.name,
        description=payload.description,
        trigger_word=payload.trigger_word,
        organization_id=_org_id(user),
        created_by=getattr(user, "id", None),
    )
    return {"id": row.id, "name": row.name}


@router.get("/datasets")
def list_datasets(db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    rows = get_dataset_service().list_datasets(db, _org_id(user))
    return {
        "count": len(rows),
        "datasets": [
            {"id": r.id, "name": r.name, "trigger_word": r.trigger_word,
             "description": r.description}
            for r in rows
        ],
    }


@router.post("/datasets/{dataset_id}/images", status_code=201)
async def upload_dataset_image(
    dataset_id: int,
    file: UploadFile = File(...),
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    content = await file.read()
    try:
        row = get_dataset_service().add_image(
            db, dataset_id, file.filename or "image.png", content,
            organization_id=_org_id(user),
        )
    except DatasetError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {"id": row.id, "filename": row.filename}


@router.get("/datasets/{dataset_id}/images")
def list_dataset_images(dataset_id: int, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    try:
        rows = get_dataset_service().list_images(db, dataset_id, _org_id(user))
    except DatasetError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {
        "count": len(rows),
        "images": [
            {"id": r.id, "filename": r.filename, "caption": r.caption,
             "width": r.width, "height": r.height}
            for r in rows
        ],
    }


@router.put("/datasets/{dataset_id}/images/{image_id}/caption")
def set_caption(dataset_id: int, image_id: int, body: dict, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    caption = str(body.get("caption", ""))
    try:
        row = get_dataset_service().set_caption(db, dataset_id, image_id, caption, _org_id(user))
    except DatasetError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {"id": row.id, "caption": row.caption}


@router.delete("/datasets/{dataset_id}/images/{image_id}")
def delete_dataset_image(dataset_id: int, image_id: int, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    try:
        deleted = get_dataset_service().delete_image(db, dataset_id, image_id, _org_id(user))
    except DatasetError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {"deleted": deleted}


@router.get("/datasets/{dataset_id}/validate")
def validate_dataset(dataset_id: int, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    try:
        return get_dataset_service().validate(db, dataset_id, _org_id(user))
    except DatasetError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/datasets/{dataset_id}/captions")
def generate_captions(dataset_id: int, payload: CaptionRequest, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    try:
        results = get_dataset_service().generate_captions(
            db, dataset_id, _org_id(user),
            model=payload.model, overwrite=payload.overwrite,
        )
    except DatasetError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except LLMError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return {"captioned": len([r for r in results if "error" not in r]), "results": results}


# --------------------------------------------------------------------------
# Training (Leonidas)
# --------------------------------------------------------------------------
@router.get("/training/status")
def training_status() -> dict:
    return LoRATrainingService().status()


@router.get("/training/presets")
def training_presets() -> dict:
    return {"presets": LoRATrainingService().presets()}


@router.get("/training/hardware")
def hardware_info() -> dict:
    return detect_hardware().to_dict()


@router.post("/training/preflight")
def preflight(options: dict, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    return advise(options).to_dict()


@router.post("/training/projects", status_code=201)
def create_training_project(payload: TrainingProjectCreateRequest, db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    row = TrainingProject(
        organization_id=_org_id(user),
        dataset_id=payload.dataset_id,
        name=payload.name,
        preset=payload.preset,
        base_model=payload.base_model or "",
        created_by=getattr(user, "id", None),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name}


@router.get("/training/projects")
def list_training_projects(db: DbSession = None, user: Annotated[object, Depends(current_user)] = None) -> dict:
    query = select(TrainingProject).order_by(TrainingProject.created_at.desc())
    org = _org_id(user)
    if org is not None:
        query = query.where(TrainingProject.organization_id == org)
    rows = db.execute(query).scalars().all()
    return {
        "count": len(rows),
        "projects": [{"id": r.id, "name": r.name, "preset": r.preset} for r in rows],
    }


__all__ = ["router"]
