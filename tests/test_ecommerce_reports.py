"""Branches of app/ecommerce/reports.py that the other report suites do not reach.

Covered here, with mail faked and no model call:
- collect_changes' severity filter, shop filter and newest-first ordering
- render_facts' price and assortment tables, including the 30-row cap
- _digest_rows in the exact shape render_report_email expects (and a report
  that carries no change ids)
- send_weekly_digest swallowing a transport error and rolling back its claim

Run with LLM_PROVIDER=test.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from app.core.database.ecommerce_models import ChangeEvent, Report, ReportStatus, Shop
from app.core.database.identity import Organization, User
from app.core.report_email import render_report_email
from app.ecommerce.reports import _digest_changes, collect_changes, render_facts, send_weekly_digest


def _org(db, plan: str = "pro") -> Organization:
    org = Organization(name=f"Reports Org {plan}", slug=uuid.uuid4().hex[:20], plan=plan)
    db.add(org)
    db.flush()
    return org


def _admin(db, org: Organization) -> User:
    user = User(
        organization_id=org.id,
        email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="not-a-real-hash",
        role="admin",
        is_active=True,
        email_verified=True,
        weekly_digest_enabled=True,
    )
    db.add(user)
    db.flush()
    return user


def _event(
    db,
    org: Organization,
    *,
    kind: str = "price_decrease",
    severity: str = "medium",
    shop_id: int | None = None,
    detected_at: datetime | None = None,
    **fields,
) -> ChangeEvent:
    data = {
        "organization_id": org.id,
        "shop_id": shop_id,
        "kind": kind,
        "severity": severity,
        "product_name": "Linen Runner",
        "competitor_name": "Claybarn Co",
        "competitor_domain": "claybarn.test",
        "title": f"{kind} of Linen Runner",
        "detected_at": detected_at or datetime(2026, 10, 1, 12, 0, 0),
    }
    data.update(fields)
    event = ChangeEvent(**data)
    db.add(event)
    return event


def _report(db, org: Organization, *, change_ids: list[int] | None = None) -> Report:
    report = Report(
        organization_id=org.id,
        kind="weekly",
        status=ReportStatus.COMPLETED,
        period_start=datetime(2026, 9, 24),
        period_end=datetime(2026, 10, 1),
        title="Weekly report",
        markdown="# Weekly report\n\nAll quiet this week.",
        facts={"shop": {"name": "Acme"}, "total_changes": 0},
        change_ids=change_ids or [],
        language="en",
    )
    db.add(report)
    db.commit()
    return report


def _move(i: int) -> dict:
    """One price_moves row in the exact shape build_facts produces."""
    return {
        "competitor": f"Competitor {i}",
        "product": f"Move {i}",
        "previous_price": Decimal(100 + i),
        "new_price": Decimal(90 + i),
        "delta": Decimal(-10),
        "delta_pct": -10.0,
        "currency": "EUR",
        "evidence_url": f"https://c{i}.test/p/{i}",
    }


class TestCollectChanges:
    def test_the_severity_filter_keeps_only_the_requested_levels(self, db_session):
        org = _org(db_session)
        base = datetime(2026, 10, 1, 12, 0, 0)
        _event(db_session, org, severity="low", detected_at=base + timedelta(hours=3))
        medium_new = _event(db_session, org, severity="medium", detected_at=base + timedelta(hours=2))
        _event(db_session, org, severity="high", detected_at=base + timedelta(hours=1))
        medium_old = _event(db_session, org, severity="medium", detected_at=base)
        db_session.commit()

        start, end = base - timedelta(days=7), base + timedelta(days=1)
        filtered = collect_changes(db_session, org.id, start, end, severities=["medium"])
        # only the two medium rows survive, newest first
        assert [c.id for c in filtered] == [medium_new.id, medium_old.id]

    def test_the_shop_filter_and_newest_first_order(self, db_session):
        org = _org(db_session)
        shop_a = Shop(organization_id=org.id, name="Shop A", url="https://a.test", domain="a.test")
        shop_b = Shop(organization_id=org.id, name="Shop B", url="https://b.test", domain="b.test")
        db_session.add_all([shop_a, shop_b])
        db_session.flush()
        base = datetime(2026, 10, 1, 12, 0, 0)
        older = _event(db_session, org, shop_id=shop_a.id, detected_at=base + timedelta(hours=1))
        newer = _event(db_session, org, shop_id=shop_b.id, detected_at=base + timedelta(hours=2))
        db_session.commit()

        start, end = base - timedelta(days=7), base + timedelta(days=1)
        all_rows = collect_changes(db_session, org.id, start, end)
        assert [c.id for c in all_rows] == [newer.id, older.id]
        only_a = collect_changes(db_session, org.id, start, end, shop_a.id)
        assert [c.id for c in only_a] == [older.id]


class TestRenderFactsTables:
    def test_the_price_table_caps_at_30_rows_and_says_so(self):
        facts = {
            "shop": {"name": "Acme"},
            "total_changes": 35,
            "by_kind": {"price_decrease": 35},
            "by_competitor": {},
            "price_moves": [_move(i) for i in range(1, 36)],
            "assortment": [],
        }
        text = render_facts(facts, "en")
        assert "| Move 1 |" in text and "| Move 30 |" in text
        assert "Move 31" not in text
        assert "_...and 5 more price moves._" in text

    def test_the_price_cap_message_is_translated(self):
        facts = {
            "shop": {"name": "Acme"},
            "total_changes": 35,
            "by_kind": {"price_decrease": 35},
            "by_competitor": {},
            "price_moves": [_move(i) for i in range(1, 36)],
            "assortment": [],
        }
        text = render_facts(facts, "nl")
        assert "_...en nog 5 prijsbewegingen._" in text

    def test_the_assortment_table_renders_kind_words_and_evidence(self):
        facts = {
            "shop": {"name": "Acme"},
            "total_changes": 2,
            "by_kind": {"new_product": 1, "mystery_kind": 1},
            "price_moves": [],
            "assortment": [
                {"competitor": "Vos", "product": "Wool Scarf", "kind": "new_product",
                 "evidence_url": "https://vos.test/scarf"},
                {"competitor": "Kade", "product": "Old Lamp", "kind": "mystery_kind", "evidence_url": ""},
            ],
        }
        text = render_facts(facts, "en")
        assert "| Competitor | Product | What happened | Evidence |" in text
        assert "| Vos | Wool Scarf | new listing | [view](https://vos.test/scarf) |" in text
        # an unknown kind is shown as-is rather than dropped or guessed at
        assert "| Kade | Old Lamp | mystery_kind | - |" in text

    def test_the_assortment_table_caps_at_30_rows_and_says_so(self):
        items = [
            {"competitor": f"C{i}", "product": f"A{i}", "kind": "new_product", "evidence_url": ""}
            for i in range(1, 36)
        ]
        facts = {
            "shop": {"name": "Acme"},
            "total_changes": 35,
            "by_kind": {"new_product": 35},
            "price_moves": [],
            "assortment": items,
        }
        text = render_facts(facts, "en")
        assert "| A30 |" in text and "A31" not in text
        assert "_...and 5 more assortment changes._" in text


class TestDigestRows:
    def test_a_report_without_change_ids_gives_no_rows(self, db_session):
        org = _org(db_session)
        report = _report(db_session, org)
        assert _digest_changes(db_session, report) == []

    def test_rows_have_the_shape_render_report_email_expects(self, db_session):
        org = _org(db_session)
        price = _event(
            db_session, org, kind="price_decrease", severity="high",
            previous_price=Decimal("34.00"), new_price=Decimal("27.50"), delta_pct=-19.1,
            source_url="https://claybarn.test/runner",
        )
        added = _event(
            db_session, org, kind="new_product", severity="medium", product_name="Wool Scarf",
            competitor_name="", competitor_domain="vos.test", source_url="",
        )
        db_session.commit()
        report = _report(db_session, org, change_ids=[price.id, added.id])

        rows = _digest_changes(db_session, report)
        assert rows == [
            {"kind": "price_decrease", "competitor": "Claybarn Co", "product": "Linen Runner",
             "evidence_url": "https://claybarn.test/runner",
             "previous_price": 34.0, "new_price": 27.5, "delta_pct": -19.1},
            # no prices on a new listing: the keys are absent, not zero
            {"kind": "new_product", "competitor": "vos.test", "product": "Wool Scarf", "evidence_url": ""},
        ]

        subject, text, html = render_report_email(
            {"period_start": "2026-09-24T00:00:00", "period_end": "2026-10-01T00:00:00",
             "facts": {"shop": {"name": "Acme"}, "by_kind": {}}},
            rows, lang="en",
        )
        assert subject == "Week 40: 2 changes at your competitors"
        assert "Linen Runner" in text and "Wool Scarf" in html


class TestDigestFailure:
    def test_a_transport_error_is_swallowed_and_the_claim_rolls_back(self, db_session, monkeypatch):
        org = _org(db_session)
        admin = _admin(db_session, org)
        report = _report(db_session, org)

        def boom(to: str, subject: str, text: str, html: str) -> bool:
            raise RuntimeError("smtp down")

        monkeypatch.setattr("app.core.email.send_rich", boom)
        # a mail failure must never fail the job that produced the report
        assert send_weekly_digest(db_session, report) is False

        sent: list[str] = []

        def ok(to: str, subject: str, text: str, html: str) -> bool:
            sent.append(to)
            return True

        monkeypatch.setattr("app.core.email.send_rich", ok)
        # the failed attempt rolled back its claim, so a retry sends exactly once
        assert send_weekly_digest(db_session, report) is True
        assert sent == [admin.email]
