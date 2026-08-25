"""Documents domain service: tenant-scoped document storage + RAG indexing.

Replaces Apollo's JSON-file DocumentService with SQL rows (tenancy, audit,
projects) while keeping the FAISS index on disk. Cross-domain consumers
(the Athena agent's ``documents.*`` tools, chat) call into this service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database.creation_models import Document, DocumentChunk
from app.documents.parsers import extract_text_from_bytes
from app.documents.rag_service import RAGService

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".txt", ".pdf", ".docx"}


class DocumentError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class RetrievedContext:
    context: str
    citations: list[dict]


class DocumentService:
    """Upload → extract → persist → index, all tenant-scoped."""

    def __init__(self, rag: RAGService | None = None) -> None:
        self.rag = rag or RAGService()

    # ---------- ingestion ----------

    def upload(
        self,
        db: Session,
        *,
        filename: str,
        content: bytes,
        organization_id: int | None,
        project_id: int | None = None,
        created_by: int | None = None,
    ) -> Document:
        extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension not in SUPPORTED_EXTENSIONS:
            raise DocumentError(
                f"Unsupported file type '{extension}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
            )
        if len(content) > 100 * 1024 * 1024:
            raise DocumentError("File too large", status_code=413)

        try:
            text = extract_text_from_bytes(filename, content)
        except ValueError as exc:
            raise DocumentError(str(exc)) from exc

        row = Document(
            organization_id=organization_id,
            project_id=project_id,
            title=filename,
            filename=filename,
            content_hash=__import__("hashlib").sha256(content).hexdigest(),
            size_bytes=len(content),
            mime_type=_guess_mime(extension),
            chunk_count=0,
            created_by=created_by,
        )
        db.add(row)
        db.flush()
        doc_id = str(row.id)

        chunks_added = self.rag.add_document(doc_id, text, {"filename": filename})
        from app.core.database.models import utcnow

        for i, chunk_text_value in enumerate(_chunk_list(text)):
            db.add(DocumentChunk(document_id=row.id, ordinal=i, text=chunk_text_value))
        row.chunk_count = chunks_added
        row.indexed_at = utcnow()
        db.commit()
        db.refresh(row)
        return row

    # ---------- management ----------

    def list_documents(self, db: Session, organization_id: int | None) -> list[Document]:
        query = select(Document).order_by(Document.created_at.desc())
        if organization_id is not None:
            query = query.where(Document.organization_id == organization_id)
        return list(db.execute(query).scalars())

    def get_document(self, db: Session, doc_id: int, organization_id: int | None) -> Document | None:
        query = select(Document).where(Document.id == doc_id)
        if organization_id is not None:
            query = query.where(Document.organization_id == organization_id)
        return db.execute(query).scalars().first()

    def delete_document(self, db: Session, doc_id: int, organization_id: int | None) -> bool:
        row = self.get_document(db, doc_id, organization_id)
        if row is None:
            return False
        self.rag.remove_document(str(doc_id))
        db.delete(row)
        db.commit()
        return True

    def clear_all(self, db: Session, organization_id: int | None) -> int:
        rows = self.list_documents(db, organization_id)
        for row in rows:
            self.rag.remove_document(str(row.id))
            db.delete(row)
        db.commit()
        return len(rows)

    # ---------- retrieval ----------

    def ask_context(self, question: str, top_k: int | None = None) -> RetrievedContext:
        chunks = self.rag.query(question, top_k=top_k)
        return RetrievedContext(
            context=self.rag.format_context(chunks),
            citations=[
                {
                    "document": c.filename,
                    "chunk": c.chunk_index,
                    "score": round(c.score, 4),
                    "excerpt": c.text[:240],
                }
                for c in chunks
            ],
        )


def _guess_mime(extension: str) -> str:
    return {
        ".txt": "text/plain",
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }.get(extension, "application/octet-stream")


def _chunk_list(text: str) -> list[str]:
    from app.documents.chunking import chunk_text
    from app.core.config import settings as s

    return chunk_text(text, chunk_size=s.rag_chunk_size, overlap=s.rag_chunk_overlap)


__all__ = ["DocumentError", "DocumentService", "RetrievedContext"]
