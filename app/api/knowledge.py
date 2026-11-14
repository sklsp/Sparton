"""Knowledge domain API: documents, RAG status, chat with citations."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import or_, select

from app.api.schemas import ChatRequest, ChatResponse, PromptTemplateCreate
from app.core.auth.api import DbSession, current_user
from app.core.database.creation_models import (
    Conversation,
    ConversationMessage,
    Document,
    PromptTemplate,
)
from app.documents.rag_service import RAGService
from app.documents.service import DocumentError, DocumentService
from app.llm import LLMError, get_llm_provider

router = APIRouter(tags=["knowledge"])

_document_service: DocumentService | None = None


def get_document_service() -> DocumentService:
    global _document_service
    if _document_service is None:
        _document_service = DocumentService(RAGService())
    return _document_service


def _org_id(user: object) -> int | None:
    return getattr(user, "organization_id", None)


@router.post(
    "/documents/upload",
    status_code=201,
    responses={401: {"description": "Authentication required"}},
)
async def upload_documents(
    db: DbSession,
    user: Annotated[object, Depends(current_user)],
    files: list[UploadFile] = File(...),
) -> dict[str, Any]:
    service = get_document_service()
    results = []
    for upload in files:
        content = await upload.read()
        try:
            row = service.upload(
                db,
                filename=upload.filename or "document.txt",
                content=content,
                organization_id=_org_id(user),
                created_by=getattr(user, "id", None),
            )
            results.append({"filename": row.filename, "id": row.id, "chunks": row.chunk_count})
        except DocumentError as exc:
            results.append({"filename": upload.filename, "error": exc.message})
    failed = [r for r in results if "error" in r]
    return {"uploaded": len(results) - len(failed), "failed": len(failed), "results": results}


@router.get("/documents", responses={401: {"description": "Authentication required"}})
def list_documents(
    db: DbSession,
    user: Annotated[object, Depends(current_user)],
    limit: int = 50,
) -> dict[str, Any]:
    query = select(Document).order_by(Document.created_at.desc()).limit(min(limit, 200))
    org = _org_id(user)
    if org is not None:
        query = query.where(Document.organization_id == org)
    rows = db.execute(query).scalars().all()
    return {
        "count": len(rows),
        "documents": [
            {"id": r.id, "title": r.title, "size_bytes": r.size_bytes,
             "chunks": r.chunk_count, "created_at": str(r.created_at)}
            for r in rows
        ],
    }


@router.delete(
    "/documents/{doc_id}",
    responses={
        401: {"description": "Authentication required"},
        404: {"description": "Document not found"},
    },
)
def delete_document(
    doc_id: int,
    db: DbSession,
    user: Annotated[object, Depends(current_user)],
) -> dict[str, Any]:
    deleted = get_document_service().delete_document(db, doc_id, _org_id(user))
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found")
    return {"deleted": True}


@router.get("/rag/status", responses={401: {"description": "Authentication required"}})
def rag_status(
    user: Annotated[object, Depends(current_user)],
) -> dict[str, Any]:
    return get_document_service().rag.status()


@router.get(
    "/rag/debug-query", responses={401: {"description": "Authentication required"}}
)
def rag_debug_query(
    q: str,
    user: Annotated[object, Depends(current_user)],
    top_k: int | None = None,
) -> dict[str, Any]:
    return get_document_service().rag.query_debug(q, top_k=top_k)


# --- chat ---------------------------------------------------------------------
DEFAULT_TEMPLATE = (
    "You are a helpful assistant. Answer using the provided document context "
    "when it is relevant, and cite sources as [n]. If the context does not "
    "contain the answer, say so plainly."
)


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={
        401: {"description": "Authentication required"},
        404: {"description": "Conversation not found"},
        502: {"description": "LLM provider error"},
    },
)
def chat(
    payload: ChatRequest,
    db: DbSession,
    user: Annotated[object, Depends(current_user)],
) -> ChatResponse:
    """RAG-augmented chat with persisted conversation history."""
    org = _org_id(user)

    conversation = None
    if payload.conversation_id is not None:
        conversation = db.get(Conversation, payload.conversation_id)
        if conversation is None or (org is not None and conversation.organization_id != org):
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        conversation = Conversation(
            organization_id=org,
            project_id=payload.project_id,
            title=payload.message[:80],
            created_by=getattr(user, "id", None),
        )
        db.add(conversation)
        db.flush()

    citations: list[dict[str, Any]] = []
    context_block = ""
    if payload.use_rag:
        retrieved = get_document_service().ask_context(payload.message, top_k=payload.top_k)
        context_block = retrieved.context
        citations = retrieved.citations

    history_rows = db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation.id)
        .order_by(ConversationMessage.id.desc())
        .limit(12)
    ).scalars().all()
    history = [
        {"role": m.role, "content": m.content} for m in reversed(history_rows)
    ]

    system_content = DEFAULT_TEMPLATE
    if context_block:
        system_content += f"\n\nDocument context:\n{context_block}"

    messages = [{"role": "system", "content": system_content}, *history,
                {"role": "user", "content": payload.message}]

    provider = get_llm_provider()
    try:
        answer = provider.complete(messages)
    except LLMError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    db.add(ConversationMessage(conversation_id=conversation.id, role="user", content=payload.message))
    db.add(ConversationMessage(
        conversation_id=conversation.id, role="assistant",
        content=answer, citations=citations,
        model=getattr(provider, "default_model", "") or "",
    ))
    db.commit()

    return ChatResponse(conversation_id=conversation.id, answer=answer, citations=citations)


@router.get(
    "/conversations/{conversation_id}/history",
    responses={
        401: {"description": "Authentication required"},
        404: {"description": "Conversation not found"},
    },
)
def conversation_history(
    conversation_id: int,
    db: DbSession,
    user: Annotated[object, Depends(current_user)],
) -> dict[str, Any]:
    conversation = db.get(Conversation, conversation_id)
    org = _org_id(user)
    if conversation is None or (org is not None and conversation.organization_id != org):
        raise HTTPException(status_code=404, detail="Conversation not found")
    rows = db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(ConversationMessage.id)
    ).scalars().all()
    return {
        "conversation_id": conversation_id,
        "messages": [
            {"role": m.role, "content": m.content, "citations": m.citations}
            for m in rows
        ],
    }


@router.delete(
    "/conversations/{conversation_id}/history",
    responses={
        401: {"description": "Authentication required"},
        404: {"description": "Conversation not found"},
    },
)
def clear_conversation(
    conversation_id: int,
    db: DbSession,
    user: Annotated[object, Depends(current_user)],
) -> dict[str, Any]:
    conversation = db.get(Conversation, conversation_id)
    org = _org_id(user)
    if conversation is None or (org is not None and conversation.organization_id != org):
        raise HTTPException(status_code=404, detail="Conversation not found")
    rows = db.execute(
        select(ConversationMessage).where(ConversationMessage.conversation_id == conversation_id)
    ).scalars().all()
    for row in rows:
        db.delete(row)
    db.commit()
    return {"cleared": True}


# --- prompt templates ------------------------------------------------------------
@router.get("/prompts", responses={401: {"description": "Authentication required"}})
def list_prompts(db: DbSession, user: Annotated[object, Depends(current_user)]) -> dict[str, Any]:
    # `organization_id IN (org, NULL)` never matches the NULL rows, because SQL
    # three-valued logic makes any comparison with NULL unknown. Global templates
    # were therefore invisible to every tenant. Use an explicit OR.
    org = _org_id(user)
    query = select(PromptTemplate)
    if org is not None:
        query = query.where(
            or_(PromptTemplate.organization_id == org, PromptTemplate.organization_id.is_(None))
        )
    rows = db.execute(query).scalars().all()
    return {
        "count": len(rows),
        "prompts": [
            {"id": r.id, "key": r.key, "name": r.name, "description": r.description}
            for r in rows
        ],
    }


@router.post(
    "/prompts",
    status_code=201,
    responses={401: {"description": "Authentication required"}},
)
def create_prompt(
    payload: PromptTemplateCreate,
    db: DbSession,
    user: Annotated[object, Depends(current_user)],
) -> dict[str, Any]:
    row = PromptTemplate(
        organization_id=_org_id(user),
        key=payload.key,
        name=payload.name,
        template=payload.template,
        description=payload.description,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "key": row.key}


__all__ = ["router"]
