"""Regression tests for crashes mypy found in the feature-flagged experimental domains."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import settings


def test_training_preflight_without_ai_toolkit_reports_blocked(monkeypatch):
    """preflight() raised AttributeError (advise.__self__) whenever AI Toolkit was not configured."""
    from app.training import service
    from app.training.preflight import PreflightReport

    monkeypatch.setattr(settings, "ai_toolkit_path", "")
    monkeypatch.setattr(service, "advise", lambda options: PreflightReport(hardware={}))
    report = service.LoRATrainingService().preflight({})
    assert report["verdict"] == "blocked"
    assert report["checks"][0]["name"] == "AI Toolkit"


def test_rag_quarantine_works_with_the_default_directory(monkeypatch, tmp_path):
    """settings.rag_dir is a str, so the default persist_dir broke `persist_dir / name` with a TypeError."""
    pytest.importorskip("numpy")  # the RAG stack is in requirements-experimental.txt
    from app.documents.rag_service import RAGService

    monkeypatch.setattr(settings, "rag_dir", str(tmp_path))
    (tmp_path / "meta.json").write_text("{}", encoding="utf-8")
    rag = RAGService(embedding_client=object())
    assert isinstance(rag.persist_dir, Path)
    rag._quarantine_files()
    assert not (tmp_path / "meta.json").exists()
    assert len(list((tmp_path / "quarantined").iterdir())) == 1
