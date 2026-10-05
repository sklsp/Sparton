"""The weekly report is written in the account's language (v1.1).

Only the prose changes language. The facts, and so every number, come from the
diff engine and are identical in Dutch and English; the model is told which
language to write and is still given the same FACTS. Run with LLM_PROVIDER=test.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database.ecommerce_models import ChangeEvent, Shop
from app.core.database.identity import Organization, User
from app.core.database.models import utcnow
from app.ecommerce.reports import build_facts, generate_report, render_facts


@pytest.fixture()
def org(client, auth_headers, db_session):
    user = db_session.execute(select(User).where(User.email == "admin@example.com")).scalars().first()
    return db_session.get(Organization, user.organization_id)


@pytest.fixture()
def shop(db_session, org):
    row = Shop(organization_id=org.id, name="Acme Homeware", url="https://acme.test", domain="acme.test")
    db_session.add(row)
    db_session.flush()
    db_session.add(ChangeEvent(
        organization_id=org.id, shop_id=row.id, kind="price_decrease", severity="high",
        product_name="Linen Table Runner", competitor_name="Claybarn Co", competitor_domain="claybarn.test",
        previous_price=Decimal("34.00"), new_price=Decimal("27.50"), delta=Decimal("-6.50"), delta_pct=-19.1,
        currency="EUR", title="price cut", evidence_url="https://claybarn.test/runner",
        detected_at=utcnow() - timedelta(hours=2),
    ))
    db_session.commit()
    return row


def _numbers(text: str) -> list[str]:
    return sorted(re.findall(r"\d+(?:[.,]\d+)?", text))


class TestTheFallbackText:
    def test_dutch_and_english_say_the_same_numbers(self, db_session, org, shop):
        changes = db_session.execute(select(ChangeEvent)).scalars().all()
        facts = build_facts(changes, shop=shop)
        en, nl = render_facts(facts, "en"), render_facts(facts, "nl")
        assert "## What changed" in en and "## Price moves" in en
        assert "## Wat er veranderde" in nl and "## Prijsbewegingen" in nl
        assert "What changed" not in nl
        assert _numbers(en) == _numbers(nl)
        assert "€34.00" in nl and "€27.50" in nl and "-19%" in nl

    def test_a_quiet_week_in_dutch(self):
        assert "Deze week bewoog er niets" in render_facts({"total_changes": 0}, "nl")

    def test_an_unknown_language_is_english(self):
        assert "## What changed" in render_facts({"total_changes": 0}, "de")


class TestTheModelWritesTheProse:
    def test_a_dutch_account_gets_dutch_instructions_and_the_same_facts(self, db_session, org, shop, llm):
        org.language = "nl"
        db_session.commit()
        nl = generate_report(db_session, organization_id=org.id, shop=shop)
        org.language = "en"
        db_session.commit()
        en = generate_report(db_session, organization_id=org.id, shop=shop)

        (nl_system, nl_user), (en_system, en_user) = llm.calls[-2], llm.calls[-1]
        assert "Dutch (Nederlands)" in nl_system["content"]
        assert "## Wat er veranderde" in nl_system["content"]
        assert "Dutch" not in en_system["content"]
        assert "in Dutch" in nl_user["content"] and "in English" in en_user["content"]

        def facts_of(message):
            blob = json.loads(message["content"].split("```json\n", 1)[1].split("\n```", 1)[0])
            blob.pop("period")
            return blob

        # The numbers the model may use are the diff engine's, whatever the language.
        assert facts_of(nl_user) == facts_of(en_user)
        assert nl.language == "nl" and en.language == "en"
        assert nl.markdown == "Deterministic response."  # the test provider's prose was kept

    def test_the_report_language_follows_the_account_unless_given(self, db_session, org, shop, llm):
        org.language = "nl"
        db_session.commit()
        assert generate_report(db_session, organization_id=org.id, shop=shop, use_llm=False).language == "nl"
        explicit = generate_report(db_session, organization_id=org.id, shop=shop, use_llm=False, language="en")
        assert explicit.language == "en" and "## What changed" in explicit.markdown

    def test_a_broken_model_still_gives_a_dutch_report(self, db_session, org, shop, monkeypatch):
        import app.llm as llm_module

        class Exploding:
            model = "n/a"

            def complete(self, *a, **k):
                raise RuntimeError("provider is down")

        monkeypatch.setattr(llm_module, "get_llm_provider", lambda: Exploding())
        org.language = "nl"
        db_session.commit()
        report = generate_report(db_session, organization_id=org.id, shop=shop)
        assert report.status == "COMPLETED"
        assert "## Wat er veranderde" in report.markdown
        assert "Linen Table Runner" in report.markdown  # names are never translated


class TestTheAccountLanguage:
    def test_signup_records_the_language_and_the_switch_changes_it(self, client, db_session):
        made = client.post("/auth/register", json={
            "email": "nl@example.com", "password": "correct-horse-battery",
            "organization_name": "Winkel", "language": "nl",
        })
        assert made.status_code == 201, made.text
        assert made.json()["user"]["language"] == "nl"
        headers = {"Authorization": f"Bearer {made.json()['token']}"}

        assert client.put("/auth/language", headers=headers, json={"language": "en"}).json() == {"language": "en"}
        assert client.get("/auth/me", headers=headers).json()["language"] == "en"
        assert client.put("/auth/language", headers=headers, json={"language": "de"}).status_code == 422
        assert client.put("/auth/language", json={"language": "nl"}).status_code == 401

    def test_signup_without_a_language_is_english(self, client, auth_headers):
        assert client.get("/auth/me", headers=auth_headers).json()["language"] == "en"

    def test_reports_say_which_language_they_are_in(self, client, auth_headers, db_session, org, shop):
        generate_report(db_session, organization_id=org.id, shop=shop, use_llm=False, language="nl")
        listed = client.get(f"/shops/{shop.id}/reports", headers=auth_headers).json()
        assert listed["reports"][0]["language"] == "nl"
