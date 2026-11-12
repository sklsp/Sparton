"""Regression tests for crashes mypy found in the feature-flagged experimental domains."""

from __future__ import annotations

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
