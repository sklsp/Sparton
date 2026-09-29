"""Pydantic request/response schemas for the SPARTON API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, EmailStr, Field


# --- auth ------------------------------------------------------------------
class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    organization_name: str = Field(default="Default", max_length=160)
    # Registration creates the first admin of a NEW organization only;
    # joining an existing org is an invite/admin flow.
    role: str = Field(default="admin", exclude=True)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    token: str
    user: dict[str, Any]


# --- email verification / password reset -----------------------------------
class VerifyRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# --- chat / documents --------------------------------------------------------
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: int | None = None
    project_id: int | None = None
    use_rag: bool = True
    top_k: int | None = Field(default=None, ge=1, le=20)


class ChatResponse(BaseModel):
    conversation_id: int
    answer: str
    citations: list[dict[str, Any]] = []


class PromptTemplateCreate(BaseModel):
    key: str = Field(min_length=1, max_length=48)
    name: str = Field(min_length=1, max_length=120)
    template: str
    description: str = ""


# --- agent -------------------------------------------------------------------
class AgentRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    session_id: str = Field(default="default", max_length=64)
    project_id: int | None = None


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    note: str | None = Field(default=None, max_length=2000)


# --- research -----------------------------------------------------------------
class ResearchJobRequest(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    start_urls: list[str] = Field(default_factory=list, max_length=20)
    project_id: int | None = None


# --- generation -----------------------------------------------------------------
class GenerationRequest(BaseModel):
    workflow_id: str
    prompt: str = Field(min_length=1, max_length=4000)
    negative_prompt: str | None = Field(default=None, max_length=4000)
    seed: int | None = Field(default=None, ge=0)
    steps: int | None = Field(default=None, ge=1, le=150)
    cfg: float | None = Field(default=None, ge=0.5, le=30.0)
    width: int | None = Field(default=None, ge=64, le=4096)
    height: int | None = Field(default=None, ge=64, le=4096)
    checkpoint: str | None = None
    lora_name: str | None = None
    lora_strength_model: float | None = Field(default=None, ge=-2.0, le=2.0)
    lora_strength_clip: float | None = Field(default=None, ge=-2.0, le=2.0)


class WorkflowImportRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    graph: dict[str, Any]
    description: str = ""
    arch: str = "generic"
    inputs: dict[str, Any] | None = None


# --- datasets / training ------------------------------------------------------
class DatasetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
    trigger_word: str = Field(default="", max_length=64)


class CaptionRequest(BaseModel):
    model: str | None = None
    overwrite: bool = False


class TrainingProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    dataset_id: int | None = None
    preset: str = "character"
    base_model: str | None = None


class TrainingStartRequest(BaseModel):
    steps: int = Field(default=2000, ge=50, le=50000)
    learning_rate: float = Field(default=1e-4, gt=0, le=0.1)
    batch_size: int = Field(default=1, ge=1, le=16)
    resolution: list[int] | None = None
    lora_rank: int = Field(default=16, ge=4, le=256)


# --- projects --------------------------------------------------------------------
class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str = ""
