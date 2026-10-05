"""D-032: the weekly report email digest.

Eligibility (plan feature + verified admin + preference), idempotency (the
atomic claim on ``Report.email_digest_sent``), language selection from the
persisted account preference, and the settings endpoints that drive it all.

Mail is captured by patching ``app.core.email.send_rich``: nothing in this file
ever touches a real transport or writes to the outbox.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from app.core.config import settings
from app.core.database.ecommerce_models import Report, ReportStatus
from app.core.database.identity import Organization, User


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
@pytest.fixture()
def captured_mail(monkeypatch):
    """Record every ``send_rich`` call instead of delivering it."""
    sent: list[dict] = []

    def fake_send_rich(to: str, subject: str, text: str, html: str) -> bool:
        sent.append({"to": to, "subject": subject, "text": text, "html": html})
        return True

    monkeypatch.setattr("app.core.email.send_rich", fake_send_rich)
    return sent


def _org(db, plan: str = "pro") -> Organization:
    org = Organization(name=f"Digest Org {plan}", slug=uuid.uuid4().hex[:20], plan=plan)
    db.add(org)
    db.flush()
    return org


def _admin(
    db,
    org: Organization,
    email: str | None = None,
    *,
    verified: bool = True,
    digest_enabled: bool = True,
    language: str | None = None,
    created_at: datetime | None = None,
) -> User:
    user = User(
        organization_id=org.id,
        email=email or f"owner-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="not-a-real-hash",
        role="admin",
        is_active=True,
        email_verified=verified,
        weekly_digest_enabled=digest_enabled,
    )
    if language is not None:
        user.language = language
    if created_at is not None:
        user.created_at = created_at
    db.add(user)
    db.flush()
    return user


def _report(db, org: Organization, *, status: str = ReportStatus.COMPLETED) -> Report:
    end = datetime(2026, 10, 5, 12, 0, 0)
    report = Report(
        organization_id=org.id,
        kind="weekly",
        status=status,
        period_start=end - timedelta(days=7),
        period_end=end,
        title="Weekly report",
        markdown="# Weekly report\n\nAll quiet this week.",
        facts={"shop": {"name": "Digest Shop"}, "total_changes": 0},
        change_ids=[],
    )
    db.add(report)
    db.commit()
    return report


def _send(db, report: Report) -> bool:
    from app.ecommerce.reports import send_weekly_digest

    return send_weekly_digest(db, report)


# ---------------------------------------------------------------------------
# Digest behaviour
# ---------------------------------------------------------------------------
class TestDigestEligibility:
    def test_eligible_plan_sends_one_mail_to_the_verified_admin(self, db_session, captured_mail):
        org = _org(db_session, plan="pro")
        admin = _admin(db_session, org)
        report = _report(db_session, org)

        assert _send(db_session, report) is True
        assert len(captured_mail) == 1
        mail = captured_mail[0]
        assert mail["to"] == admin.email
        assert "Digest Shop" in mail["html"]
        db_session.refresh(report)
        assert report.email_digest_sent is True

    def test_free_plan_is_skipped(self, db_session, captured_mail):
        org = _org(db_session, plan="free")
        _admin(db_session, org)
        report = _report(db_session, org)

        assert _send(db_session, report) is False
        assert captured_mail == []
        db_session.refresh(report)
        # Not claimed: a later upgrade + retry may still send it.
        assert report.email_digest_sent is False

    def test_preference_off_is_skipped(self, db_session, captured_mail):
        org = _org(db_session, plan="pro")
        _admin(db_session, org, digest_enabled=False)
        report = _report(db_session, org)

        assert _send(db_session, report) is False
        assert captured_mail == []

    def test_unverified_admin_is_skipped(self, db_session, captured_mail):
        org = _org(db_session, plan="pro")
        _admin(db_session, org, verified=False)
        report = _report(db_session, org)

        assert _send(db_session, report) is False
        assert captured_mail == []

    def test_incomplete_report_is_skipped(self, db_session, captured_mail):
        org = _org(db_session, plan="pro")
        _admin(db_session, org)
        report = _report(db_session, org, status=ReportStatus.GENERATING)

        assert _send(db_session, report) is False
        assert captured_mail == []


class TestDigestRecipient:
    def test_the_oldest_verified_admin_receives_it(self, db_session, captured_mail):
        org = _org(db_session, plan="pro")
        now = datetime(2026, 1, 1)
        older = _admin(db_session, org, created_at=now)
        newer = _admin(db_session, org, created_at=now + timedelta(days=30))
        unverified = _admin(db_session, org, verified=False, created_at=now - timedelta(days=90))
        report = _report(db_session, org)

        assert _send(db_session, report) is True
        # Oldest *verified* admin: the unverified one is older but ineligible.
        assert captured_mail[0]["to"] == older.email
        assert newer.email not in [m["to"] for m in captured_mail]
        assert unverified.email not in [m["to"] for m in captured_mail]


class TestDigestIdempotency:
    def test_a_retry_after_success_does_not_double_send(self, db_session, captured_mail):
        org = _org(db_session, plan="pro")
        _admin(db_session, org)
        report = _report(db_session, org)

        assert _send(db_session, report) is True
        # The job retries (worker crash after the mail went out, queue redelivery...):
        # the atomic claim has already flipped, so nothing more can be sent.
        assert _send(db_session, report) is False
        assert len(captured_mail) == 1

    def test_a_previously_claimed_report_is_never_sent(self, db_session, captured_mail):
        org = _org(db_session, plan="pro")
        _admin(db_session, org)
        report = _report(db_session, org)
        report.email_digest_sent = True
        db_session.commit()

        assert _send(db_session, report) is False
        assert captured_mail == []


class TestDigestLanguage:
    def test_english_preference_renders_an_english_email(self, db_session, captured_mail):
        org = _org(db_session, plan="pro")
        _admin(db_session, org, language="en")
        report = _report(db_session, org)

        assert _send(db_session, report) is True
        mail = captured_mail[0]
        assert '<html lang="en">' in mail["html"]
        assert "nothing moved at your competitors" in mail["subject"]

    def test_the_default_is_dutch(self, db_session, captured_mail):
        org = _org(db_session, plan="pro")
        _admin(db_session, org)  # language left at its default ("nl")
        report = _report(db_session, org)

        assert _send(db_session, report) is True
        mail = captured_mail[0]
        assert '<html lang="nl">' in mail["html"]
        assert "niets bewogen bij je concurrenten" in mail["subject"]


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------
class TestSettingsEndpoints:
    def test_me_exposes_the_account_preferences(self, client, auth_headers):
        body = client.get("/auth/me", headers=auth_headers).json()
        assert body["language"] == "nl"  # the product's home market is the default
        assert body["weekly_digest_enabled"] is True

    def test_patch_settings_persists_both_fields(self, client, auth_headers):
        response = client.patch(
            "/auth/settings",
            headers=auth_headers,
            json={"language": "en", "weekly_digest_enabled": False},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["language"] == "en"
        assert body["weekly_digest_enabled"] is False

        # A fresh request re-reads the row: this proves persistence, not echo.
        again = client.get("/auth/me", headers=auth_headers).json()
        assert again["language"] == "en"
        assert again["weekly_digest_enabled"] is False

    def test_patch_settings_accepts_a_partial_update(self, client, auth_headers):
        response = client.patch(
            "/auth/settings", headers=auth_headers, json={"weekly_digest_enabled": False}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["language"] == "nl"  # untouched
        assert body["weekly_digest_enabled"] is False

    def test_patch_rejects_an_unknown_language(self, client, auth_headers):
        response = client.patch(
            "/auth/settings", headers=auth_headers, json={"language": "fr"}
        )
        assert response.status_code == 422

    def test_machine_principal_cannot_update_settings(self, client, monkeypatch):
        # conftest blanks API_KEY; enable the machine principal for this test.
        monkeypatch.setattr(settings, "api_key", "test-machine-key")
        response = client.patch(
            "/auth/settings",
            headers={"X-API-Key": "test-machine-key"},
            json={"language": "en"},
        )
        assert response.status_code == 403

