"""Billing: plan limits, Stripe protocol, and the webhook.

Every Stripe HTTP call is mocked. The tests that matter most are the
*negative* ones: a forged webhook, a replayed webhook, an unknown price, and a
cancelled subscription must all fail in the right direction (D-014).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import cast
from urllib.parse import parse_qsl

import pytest
import requests
from fastapi import HTTPException
from sqlalchemy import select

from app.billing.plans import (
    BUSINESS,
    FREE,
    PLANS,
    PRO,
    all_plans,
    check_competitors,
    check_crawl_frequency,
    check_shops,
    check_tokens,
    counts,
    get_plan,
    usage_report,
)
from app.billing.stripe import StripeClient, StripeError, parse_event, verify_signature
from app.core.database.billing_models import (
    Invoice,
    StripeEvent,
    Subscription,
    SubscriptionStatus,
)
from app.core.database.ecommerce_models import Shop
from app.core.database.identity import Organization, User
from app.core.database.models import utcnow
from app.core.database.usage_models import LLMUsage

WEBHOOK_SECRET = "whsec_test_secret"


# ---------------------------------------------------------------------------
# Plan definitions
# ---------------------------------------------------------------------------
class TestPlanDefinitions:
    def test_three_plans_with_the_agreed_prices(self):
        assert [p["id"] for p in all_plans()] == [FREE, PRO, BUSINESS]
        assert PLANS[FREE].price_cents == 0
        assert PLANS[PRO].price_cents == 2900
        assert PLANS[BUSINESS].price_cents == 7900

    def test_the_agreed_allowances(self):
        assert (PLANS[FREE].shops, PLANS[FREE].competitors) == (1, 3)
        assert (PLANS[PRO].shops, PLANS[PRO].competitors) == (3, 15)
        assert (PLANS[BUSINESS].shops, PLANS[BUSINESS].competitors) == (10, 50)

    def test_unknown_plan_fails_closed_to_free(self):
        """An unreadable plan id must never produce an unlimited allowance."""
        assert get_plan("enterprise-ultra").id == FREE
        assert get_plan(None).id == FREE
        assert get_plan("").id == FREE

    def test_plan_lookup_is_case_insensitive(self):
        assert get_plan("PRO").id == PRO


# ---------------------------------------------------------------------------
# Server-side enforcement
# ---------------------------------------------------------------------------
@pytest.fixture()
def org_id(db_session):
    from app.core.auth.service import unique_slug

    org = Organization(name="Billing Test", slug=unique_slug(db_session, "Billing Test"))
    db_session.add(org)
    db_session.flush()
    return org.id


def seed_shops(db, org, n):
    for i in range(n):
        db.add(Shop(
            organization_id=org, name=f"Shop {i}", url=f"https://s{i}.example",
            domain=f"s{i}.example",
        ))
    db.commit()


class TestEnforcement:
    def test_free_plan_allows_one_shop(self, db_session, org_id):
        seed_shops(db_session, org_id, 1)
        # The second is refused.
        with pytest.raises(HTTPException) as exc:
            check_shops(db_session, org_id, FREE)
        assert exc.value.status_code == 402
        assert "1 shop" in str(exc.value.detail)

    def test_a_higher_plan_permits_more(self, db_session, org_id):
        """Free allows 1 shop, Pro 3, Business 10 (D-013).

        `check_shops` refuses *at* the limit, because the question it answers is
        "may I add one more?".
        """
        seed_shops(db_session, org_id, 3)
        with pytest.raises(HTTPException) as free_error:
            check_shops(db_session, org_id, FREE)
        assert free_error.value.status_code == 402
        with pytest.raises(HTTPException) as pro_error:
            check_shops(db_session, org_id, PRO)
        assert pro_error.value.status_code == 402

    def test_competitor_limit(self, db_session, org_id):
        from app.core.database.ecommerce_models import Competitor

        for i in range(3):
            db_session.add(Competitor(
                organization_id=org_id, domain=f"c{i}.example", is_active=True
            ))
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            check_competitors(db_session, org_id, FREE)
        assert exc.value.status_code == 402

    def test_daily_token_limit(self, db_session, org_id):
        db_session.add(LLMUsage(
            organization_id=org_id, provider="test", model="m", task="report",
            prompt_tokens=1_900, completion_tokens=200, total_tokens=2_100,
        ))
        db_session.commit()
        with pytest.raises(HTTPException) as exc:
            check_tokens(db_session, org_id, FREE)
        assert exc.value.status_code == 402
        check_tokens(db_session, org_id, PRO)  # Pro's allowance is higher

    def test_token_limit_is_measured_from_midnight(self, db_session, org_id):
        from datetime import timedelta

        db_session.add(LLMUsage(
            organization_id=org_id, provider="test", model="m", task="report",
            total_tokens=1_900, created_at=utcnow() - timedelta(days=1),
        ))
        db_session.commit()
        # Yesterday's spend does not count against today.
        check_tokens(db_session, org_id, FREE)

    def test_enforcement_never_counts_another_tenant(self, db_session, org_id):
        """A billing check that leaked cross-tenant counts would be exploitable."""
        from app.core.auth.service import unique_slug
        from app.core.database.ecommerce_models import Competitor

        other = Organization(name="Other", slug=unique_slug(db_session, "Other Co"))
        db_session.add(other)
        db_session.flush()
        for i in range(20):
            db_session.add(Competitor(
                organization_id=other.id, domain=f"x{i}.example", is_active=True
            ))
        db_session.commit()
        # This tenant has zero competitors, so the free allowance is intact.
        check_competitors(db_session, org_id, FREE)

    def test_crawl_frequency_is_clamped_by_plan(self):
        """The plan sets the fastest cadence; a request can only slow a shop down."""
        assert check_crawl_frequency(FREE, 1) == 168      # never faster than weekly
        assert check_crawl_frequency(FREE, 24) == 168     # clamped to weekly
        assert check_crawl_frequency(FREE, 6) == 168
        assert check_crawl_frequency(PRO, 24) == 24
        assert check_crawl_frequency(PRO, 6) == 24
        assert check_crawl_frequency(PRO, 168) == 168     # slower is kept
        assert check_crawl_frequency(PRO, None) == 24     # no request: the plan's cadence
        assert check_crawl_frequency(BUSINESS, None) == 12
        assert check_crawl_frequency(BUSINESS, 2000) == 720

    def test_usage_report_shape(self, db_session, org_id):
        report = usage_report(db_session, org_id, FREE)
        assert report["plan"]["id"] == FREE
        assert set(report["used"]) == {"shops", "competitors", "daily_tokens"}
        assert report["remaining"]["shops"] == 1

    def test_counts_without_an_organization_are_zero(self, db_session, org_id):
        """`counts(None)` must not fall back to an unscoped query that would
        count every shop in the table."""
        seed_shops(db_session, org_id, 3)
        assert counts(db_session, None) == {"shops": 0, "competitors": 0, "daily_tokens": 0}


# ---------------------------------------------------------------------------
# Stripe signature verification
# ---------------------------------------------------------------------------
def sign(payload: bytes, secret: str = WEBHOOK_SECRET, at: int | None = None) -> str:
    timestamp = at if at is not None else int(time.time())
    digest = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={timestamp},v1={digest}"


class TestSignatureVerification:
    def test_a_correct_signature_validates(self):
        body = b'{"id":"evt_1","type":"invoice.paid"}'
        assert verify_signature(body, sign(body), WEBHOOK_SECRET) is True

    def test_a_wrong_secret_is_rejected(self):
        body = b'{"id":"evt_1"}'
        assert verify_signature(body, sign(body, "whsec_wrong"), WEBHOOK_SECRET) is False

    def test_a_tampered_body_is_rejected(self):
        body = b'{"id":"evt_1","amount":10}'
        header = sign(body)
        assert verify_signature(body + b"x", header, WEBHOOK_SECRET) is False

    def test_a_missing_header_is_rejected(self):
        assert verify_signature(b"{}", "", WEBHOOK_SECRET) is False

    def test_a_missing_secret_is_rejected(self):
        """Never default to "accept everything"."""
        assert verify_signature(b"{}", "t=1,v1=deadbeef", "") is False

    def test_a_replayed_old_request_is_rejected(self):
        """A captured webhook must not be replayable forever."""
        body = b'{"id":"evt_old"}'
        stale = int(time.time()) - 4000
        assert verify_signature(body, sign(body, at=stale), WEBHOOK_SECRET) is False

    def test_a_future_timestamp_is_rejected(self):
        body = b'{"id":"evt_future"}'
        assert verify_signature(
            body, sign(body, at=int(time.time()) + 4000), WEBHOOK_SECRET
        ) is False

    def test_rotation_sends_several_v1_values_and_any_may_match(self):
        body = b'{"id":"evt_rot"}'
        at = int(time.time())
        good = sign(body, at=at)
        stale = "v1=" + "0" * 64
        assert verify_signature(body, f"{stale},{good}", WEBHOOK_SECRET) is True

    def test_malformed_header_is_rejected(self):
        assert verify_signature(b"{}", "garbage", WEBHOOK_SECRET) is False
        assert verify_signature(b"{}", "v1=abc", WEBHOOK_SECRET) is False

    def test_parse_event_rejects_non_json(self):
        with pytest.raises(StripeError):
            parse_event(b"<html>not json</html>")


# ---------------------------------------------------------------------------
# The webhook is the only thing that grants a paid plan
# ---------------------------------------------------------------------------
def subscription_event(event_id: str, org_id: int, price_id: str, status: str = "active"):
    return {
        "id": event_id,
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_123",
                "customer": "cus_123",
                "status": status,
                "cancel_at_period_end": False,
                "current_period_start": 1_700_000_000,
                "current_period_end": 1_702_592_000,
                "metadata": {"organization_id": str(org_id)},
                "items": {"data": [{"price": {"id": price_id}}]},
            }
        },
    }


@pytest.fixture()
def stripe_env(monkeypatch):
    """Point the plan mapping at known fake price ids."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro_123", raising=False)
    monkeypatch.setattr(settings, "stripe_price_business", "price_business_123", raising=False)
    monkeypatch.setattr(settings, "stripe_webhook_secret", WEBHOOK_SECRET, raising=False)
    return settings


class TestWebhookHandler:
    def test_subscription_update_grants_the_plan(self, db_session, org_id, stripe_env):
        from app.billing.service import handle_event

        outcome = handle_event(
            db_session, subscription_event("evt_a", org_id, "price_pro_123")
        )
        assert outcome == "updated"

        row = db_session.execute(select(Subscription)).scalars().one()
        assert row.plan == PRO
        assert row.status == SubscriptionStatus.ACTIVE
        # The denormalised copy on the organization is updated too.
        assert db_session.get(Organization, org_id).plan == PRO

    def test_a_replayed_event_is_a_no_op(self, db_session, org_id, stripe_env):
        """Stripe delivers at least once; a retry must not re-apply."""
        from app.billing.service import handle_event

        event = subscription_event("evt_dup", org_id, "price_pro_123")
        assert handle_event(db_session, event) == "updated"
        # Stripe redelivers with the same id, this time cancelling.
        replayed = dict(event)
        replayed["data"] = dict(event["data"])
        replayed["data"]["object"] = dict(event["data"]["object"])
        replayed["data"]["object"]["status"] = "canceled"

        assert handle_event(db_session, replayed) == "duplicate"
        row = db_session.execute(select(Subscription)).scalars().one()
        assert row.status == SubscriptionStatus.ACTIVE, "a replay changed the plan"

    def test_cancellation_drops_the_plan(self, db_session, org_id, stripe_env):
        from app.billing.service import handle_event

        handle_event(db_session, subscription_event("evt_b", org_id, "price_business_123"))
        assert db_session.get(Organization, org_id).plan == BUSINESS

        deleted = subscription_event("evt_c", org_id, "price_business_123")
        deleted["type"] = "customer.subscription.deleted"
        handle_event(db_session, deleted)

        row = db_session.execute(select(Subscription)).scalars().one()
        assert row.status == SubscriptionStatus.CANCELED
        assert db_session.get(Organization, org_id).plan == FREE

    def test_a_stale_update_cannot_revive_a_cancelled_subscription(self, db_session, org_id, stripe_env):
        """Stripe does not deliver in order: an old 'updated' arriving after 'deleted' must not re-grant."""
        from app.billing.service import handle_event

        handle_event(db_session, subscription_event("evt_s1", org_id, "price_business_123"))
        deleted = subscription_event("evt_s2", org_id, "price_business_123")
        deleted["type"] = "customer.subscription.deleted"
        handle_event(db_session, deleted)

        # The earlier "active" update, retried late under its own event id.
        handle_event(db_session, subscription_event("evt_s3", org_id, "price_business_123"))
        row = db_session.execute(select(Subscription)).scalars().one()
        assert row.status == SubscriptionStatus.CANCELED
        assert db_session.get(Organization, org_id).plan == FREE

    def test_updates_apply_the_live_subscription_not_the_event_copy(self, db_session, org_id, stripe_env):
        from app.billing.service import handle_event

        class LiveStripe:
            def get_subscription(self, subscription_id):
                assert subscription_id == "sub_123"
                live = subscription_event("x", org_id, "price_pro_123")["data"]["object"]
                return {**live, "status": "past_due"}

        # The event says Business and active; Stripe says it is now Pro and past due.
        event = subscription_event("evt_l1", org_id, "price_business_123")
        handle_event(db_session, event, client=cast(StripeClient, LiveStripe()))
        row = db_session.execute(select(Subscription)).scalars().one()
        assert row.stripe_price_id == "price_pro_123"
        assert row.status == "past_due"

    def test_an_old_subscription_ending_cannot_cancel_the_new_one(self, db_session, org_id, stripe_env):
        """A customer whose first subscription lapsed subscribes again and gets a new
        id. When the old one is deleted later, the new paid plan must stay."""
        from app.billing.service import handle_event

        old = subscription_event("evt_o1", org_id, "price_pro_123", status="unpaid")
        handle_event(db_session, old)
        new = subscription_event("evt_o2", org_id, "price_business_123")
        new["data"]["object"]["id"] = "sub_new"
        handle_event(db_session, new)

        old_deleted = subscription_event("evt_o3", org_id, "price_pro_123")
        old_deleted["type"] = "customer.subscription.deleted"
        handle_event(db_session, old_deleted)
        row = db_session.execute(select(Subscription)).scalars().one()
        assert (row.stripe_subscription_id, row.status) == ("sub_new", SubscriptionStatus.ACTIVE)
        assert db_session.get(Organization, org_id).plan == BUSINESS

    def test_an_unknown_price_fails_closed_to_free(self, db_session, org_id, stripe_env):
        """A typo in a Stripe price id must not grant unlimited access."""
        from app.billing.service import handle_event

        handle_event(db_session, subscription_event("evt_d", org_id, "price_mystery"))
        row = db_session.execute(select(Subscription)).scalars().one()
        assert row.plan == FREE

    def test_an_unmapped_event_does_not_crash(self, db_session, stripe_env):
        from app.billing.service import handle_event

        event = subscription_event("evt_e", 999_999, "price_pro_123")
        assert handle_event(db_session, event) == "unmapped"
        # Still recorded, so a retry is not attempted forever.
        assert db_session.execute(select(StripeEvent)).scalars().first() is not None

    def test_an_unknown_event_type_is_acknowledged(self, db_session, stripe_env):
        """Stripe adds event types; erroring on them causes infinite retries."""
        from app.billing.service import handle_event

        outcome = handle_event(db_session, {
            "id": "evt_f", "type": "radar.early_fraud_warning.created",
            "data": {"object": {}},
        })
        assert outcome == "ignored"

    def test_invoice_is_recorded_once(self, db_session, org_id, stripe_env):
        from app.billing.service import handle_event

        event = {
            "id": "evt_g", "type": "invoice.paid",
            "data": {"object": {
                "id": "in_1", "status": "paid", "amount_paid": 2900,
                "currency": "eur", "hosted_invoice_url": "https://stripe/in/1",
                "metadata": {"organization_id": str(org_id)},
            }},
        }
        handle_event(db_session, event)
        handle_event(db_session, {**event, "id": "evt_h"})

        rows = db_session.execute(select(Invoice)).scalars().all()
        assert len(rows) == 1, "an immutable invoice was recorded twice"
        assert rows[0].amount_cents == 2900


# ---------------------------------------------------------------------------
# The webhook route, over HTTP
# ---------------------------------------------------------------------------
class TestWebhookEndpoint:
    def test_unsigned_request_is_rejected(self, client, stripe_env):
        response = client.post(
            "/stripe/webhook", content=b'{"id":"evt_x","type":"invoice.paid"}'
        )
        assert response.status_code == 400
        assert "signature" in response.json()["detail"].lower()

    def test_bad_signature_is_rejected(self, client, stripe_env):
        body = b'{"id":"evt_x","type":"invoice.paid"}'
        response = client.post(
            "/stripe/webhook", content=body,
            headers={"stripe-signature": sign(body, "whsec_wrong")},
        )
        assert response.status_code == 400

    def test_validly_signed_request_is_accepted(self, client, auth_headers,
                                                db_session, stripe_env):
        from app.core.auth.service import unique_slug

        org = Organization(name="Hooked", slug=unique_slug(db_session, "Hooked"))
        db_session.add(org)
        db_session.flush()
        db_session.commit()
        org_id = org.id

        body = json.dumps(
            subscription_event("evt_http", org_id, "price_pro_123")
        ).encode()
        response = client.post(
            "/stripe/webhook", content=body,
            headers={"stripe-signature": sign(body)},
        )
        assert response.status_code == 200, response.text
        assert response.json()["received"] is True

        # The request ran in its own session, so expire ours before re-reading.
        db_session.expire_all()
        assert db_session.get(Organization, org_id).plan == PRO

    def test_unconfigured_webhook_is_a_503_not_an_open_door(self, client, monkeypatch):
        """A missing secret must refuse, never default to accepting."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "stripe_webhook_secret", None, raising=False)
        body = b'{"id":"evt_x"}'
        response = client.post(
            "/stripe/webhook", content=body,
            headers={"stripe-signature": sign(body)},
        )
        assert response.status_code == 503

    def test_oversized_body_is_rejected(self, client, stripe_env):
        from app.api.billing import MAX_WEBHOOK_BYTES

        body = b"x" * (MAX_WEBHOOK_BYTES + 100)
        response = client.post(
            "/stripe/webhook", content=body,
            headers={"stripe-signature": sign(body)},
        )
        assert response.status_code == 413

    def test_signed_but_unparseable_body_is_a_400(self, client, stripe_env):
        """A valid signature does not make a non-JSON body an event."""
        body = b"<html>not json</html>"
        response = client.post(
            "/stripe/webhook", content=body,
            headers={"stripe-signature": sign(body)},
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Webhook body was not valid JSON"

    def test_a_failed_event_is_a_500_so_stripe_retries(self, client, stripe_env,
                                                        monkeypatch):
        """A transient failure must not cost a customer the plan they paid for."""
        def boom(db, event):
            raise RuntimeError("database down")

        monkeypatch.setattr("app.api.billing.handle_event", boom)
        body = json.dumps(subscription_event("evt_fail", 1, "price_pro_123")).encode()
        response = client.post(
            "/stripe/webhook", content=body,
            headers={"stripe-signature": sign(body)},
        )
        assert response.status_code == 500
        assert response.json()["detail"] == "Could not apply the event: database down"


# ---------------------------------------------------------------------------
# Billing API surface
# ---------------------------------------------------------------------------
class TestBillingApi:
    def test_plans_are_public(self, client, stripe_env):
        """The landing page renders the pricing table before signup."""
        response = client.get("/billing/plans")
        assert response.status_code == 200
        assert len(response.json()["plans"]) == 3

    def test_plan_endpoint_requires_auth(self, client):
        assert client.get("/billing/plan").status_code == 401

    def test_plan_endpoint_reports_usage(self, client, auth_headers, stripe_env):
        body = client.get("/billing/plan", headers=auth_headers).json()
        assert body["plan"]["id"] == FREE
        assert body["used"]["shops"] == 0
        assert body["subscription"] is None

    def test_checkout_without_stripe_configured_is_503(self, client, auth_headers,
                                                        monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "stripe_secret_key", None, raising=False)
        response = client.post("/billing/checkout", headers=auth_headers,
                               json={"plan": "pro"})
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]

    def test_checkout_rejects_an_unknown_plan(self, client, auth_headers):
        response = client.post("/billing/checkout", headers=auth_headers,
                               json={"plan": "enterprise"})
        assert response.status_code == 422

    def test_portal_without_a_subscription_is_400(self, client, auth_headers):
        response = client.post("/billing/portal", headers=auth_headers)
        assert response.status_code == 400

    @pytest.fixture()
    def checkout_env(self, client, auth_headers, stripe_env, monkeypatch):
        """Billing on and Stripe faked. Returns the caller's org id and the
        Checkout sessions that were started."""
        monkeypatch.setattr(stripe_env, "billing_enabled", True, raising=False)
        monkeypatch.setattr(stripe_env, "stripe_secret_key", "sk_test_fake", raising=False)
        calls = []

        def _record_checkout(self, **kw):
            calls.append(kw)
            return "https://checkout.stripe.com/c/pay/cs_1"

        monkeypatch.setattr(StripeClient, "create_checkout_session", _record_checkout)
        monkeypatch.setattr(StripeClient, "create_customer", lambda self, *a, **kw: "cus_new")
        org_id = client.get("/auth/me", headers=auth_headers).json()["organization_id"]
        return org_id, calls

    def test_checkout_refuses_a_second_subscription(self, client, auth_headers,
                                                     checkout_env, db_session):
        """Checkout always starts a new subscription, so a Pro customer choosing
        Business there would pay for both. Plan changes go to the portal."""
        org_id, calls = checkout_env
        db_session.add(Subscription(
            organization_id=org_id, plan=PRO, status=SubscriptionStatus.ACTIVE,
            stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
        ))
        db_session.commit()

        response = client.post("/billing/checkout", headers=auth_headers,
                               json={"plan": "business"})
        assert response.status_code == 409
        assert "portal" in response.json()["detail"]
        assert calls == []

    @pytest.mark.parametrize("status, subscription_id", [
        (SubscriptionStatus.CANCELED, "sub_old"),  # subscribing again after a cancellation
        (SubscriptionStatus.ACTIVE, None),  # a Checkout that was never finished
    ])
    def test_checkout_is_allowed_without_a_live_subscription(
        self, client, auth_headers, checkout_env, db_session, status, subscription_id
    ):
        org_id, calls = checkout_env
        db_session.add(Subscription(
            organization_id=org_id, status=status,
            stripe_customer_id="cus_1", stripe_subscription_id=subscription_id,
        ))
        db_session.commit()

        response = client.post("/billing/checkout", headers=auth_headers,
                               json={"plan": "pro"})
        assert response.status_code == 200, response.text
        assert [(c["customer_id"], c["price_id"]) for c in calls] == [("cus_1", "price_pro_123")]

    def test_usage_without_an_organization_returns_all_zeros(self, client, monkeypatch):
        """A principal without an organization must not fall back to
        `organization_id IS NULL`: that would return every unscoped usage row.
        No org means no usage."""
        from app.core.config import settings

        # The machine principal is the only authenticated caller with
        # organization_id=None; a real User always has one (NOT NULL).
        monkeypatch.setattr(settings, "api_key", "test-machine-key")
        response = client.get("/billing/usage", headers={"X-API-Key": "test-machine-key"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["days"], body["requests"], body["total_tokens"]) == (30, 0, 0)
        assert body["estimated_cost_usd"] == 0.0
        assert body["by_task"] == {} and body["by_model"] == {}

    def test_usage_days_is_clamped_to_the_supported_window(self, client, auth_headers, monkeypatch):
        """The route clamps `days` before querying: 0 becomes 1 and anything
        above a year is cut to 365. The fake records what it actually got."""
        from app.api import billing as billing_api

        calls = []

        def fake_usage_summary(db, organization_id, *, days=30):
            calls.append(days)
            return {"days": days, "requests": 1, "total_tokens": 2,
                    "estimated_cost_usd": 0.0, "by_task": {}, "by_model": {}}

        monkeypatch.setattr(billing_api, "usage_summary", fake_usage_summary)

        low = client.get("/billing/usage?days=0", headers=auth_headers)
        assert low.status_code == 200, low.text
        high = client.get("/billing/usage?days=1000", headers=auth_headers)
        assert high.status_code == 200, high.text

        assert calls == [1, 365]

    def test_invoices_without_an_organization_return_empty(self, client, db_session, monkeypatch):
        """`organization_id IS NULL` would return every unscoped invoice in the
        table; no organization must mean zero rows."""
        from app.core.config import settings

        db_session.add(Invoice(
            organization_id=None, stripe_invoice_id="in_unscoped", status="paid",
            amount_cents=1000, currency="eur",
        ))
        db_session.commit()

        monkeypatch.setattr(settings, "api_key", "test-machine-key")
        response = client.get("/billing/invoices", headers={"X-API-Key": "test-machine-key"})
        assert response.status_code == 200, response.text
        assert response.json() == {"count": 0, "invoices": []}

    def test_invoices_list_the_callers_organization_newest_first(self, client, auth_headers, db_session):
        from datetime import timedelta

        org_id = db_session.execute(
            select(User).where(User.email == "admin@example.com")
        ).scalars().first().organization_id
        now = utcnow()
        db_session.add(Invoice(
            organization_id=org_id, stripe_invoice_id="in_old", status="paid",
            amount_cents=500, currency="eur", hosted_invoice_url="https://inv/old",
            invoice_pdf="https://pdf/old", created_at=now - timedelta(days=30),
        ))
        db_session.add(Invoice(
            organization_id=org_id, stripe_invoice_id="in_new", status="open",
            amount_cents=2900, currency="eur", hosted_invoice_url="https://inv/new",
            invoice_pdf="https://pdf/new", created_at=now - timedelta(days=1),
        ))
        db_session.commit()

        response = client.get("/billing/invoices", headers=auth_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == 2
        assert [i["id"] for i in body["invoices"]] == ["in_new", "in_old"]
        newest = body["invoices"][0]
        assert (newest["status"], newest["amount_cents"], newest["currency"]) == ("open", 2900, "eur")
        assert newest["url"] == "https://inv/new"
        assert newest["pdf"] == "https://pdf/new"

    def test_checkout_without_an_organization_is_rejected(self, client, monkeypatch):
        """No organization means there is nowhere to attach a subscription."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "api_key", "test-machine-key")
        response = client.post("/billing/checkout", headers={"X-API-Key": "test-machine-key"},
                               json={"plan": "pro"})
        assert response.status_code == 400
        assert "No organization" in response.json()["detail"]

    def test_checkout_is_refused_when_billing_is_disabled(self, client, auth_headers, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "billing_enabled", False)
        response = client.post("/billing/checkout", headers=auth_headers, json={"plan": "pro"})
        assert response.status_code == 503
        assert "Billing is not enabled" in response.json()["detail"]

    def test_checkout_is_refused_when_the_plan_price_is_missing(self, client, auth_headers, monkeypatch):
        """Stripe configured but the plan's price env var empty: refuse loudly
        instead of starting a session that cannot charge."""
        from app.core.config import settings

        monkeypatch.setattr(settings, "billing_enabled", True)
        monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_fake")
        monkeypatch.setattr(settings, "stripe_price_pro", None)
        response = client.post("/billing/checkout", headers=auth_headers, json={"plan": "pro"})
        assert response.status_code == 503
        assert "No Stripe price is configured for the pro plan" in response.json()["detail"]

    def test_checkout_creates_a_stripe_customer_and_subscription_row(
        self, client, auth_headers, checkout_env, db_session, monkeypatch
    ):
        """A first-time buyer gets a Stripe customer made from the caller's
        email and the org name, and a Subscription row that keeps its id so
        later checkouts reuse it."""
        org_id, calls = checkout_env
        created = []

        def fake_create_customer(self, email, name="", metadata=None):
            created.append((email, name, metadata))
            return "cus_new"

        monkeypatch.setattr(StripeClient, "create_customer", fake_create_customer)

        response = client.post("/billing/checkout", headers=auth_headers, json={"plan": "pro"})
        assert response.status_code == 200, response.text
        assert response.json() == {"url": "https://checkout.stripe.com/c/pay/cs_1", "plan": "pro"}

        # The customer is built from the caller's email and the org name.
        assert created == [("admin@example.com", "Test Org", {"organization_id": org_id})]
        # ...and the checkout session is opened for that new customer.
        assert [(c["customer_id"], c["price_id"]) for c in calls] == [("cus_new", "price_pro_123")]

        db_session.expire_all()
        row = db_session.execute(select(Subscription)).scalars().one()
        # The row keeps the customer id; the plan stays free until the webhook (D-014).
        assert row.stripe_customer_id == "cus_new"
        assert row.plan == "free"
        assert row.stripe_subscription_id is None

    def test_checkout_reuses_an_existing_stripe_customer(
        self, client, auth_headers, checkout_env, db_session, monkeypatch
    ):
        """A second checkout for the same org must not create another Stripe
        customer: the id on the Subscription row wins."""
        org_id, calls = checkout_env
        db_session.add(Subscription(
            organization_id=org_id, status=SubscriptionStatus.CANCELED,
            stripe_customer_id="cus_1", stripe_subscription_id=None,
        ))
        db_session.commit()

        def refuse(self, *args, **kwargs):
            raise AssertionError("create_customer must not run for an existing customer")

        monkeypatch.setattr(StripeClient, "create_customer", refuse)

        response = client.post("/billing/checkout", headers=auth_headers, json={"plan": "pro"})
        assert response.status_code == 200, response.text
        assert [(c["customer_id"], c["price_id"]) for c in calls] == [("cus_1", "price_pro_123")]

    def test_portal_returns_the_session_url_for_a_subscribed_organization(
        self, client, auth_headers, db_session, monkeypatch
    ):
        """A customer with a Stripe id gets the Customer Portal URL to manage
        payment and plan."""
        from app.core.config import settings

        org_id = client.get("/auth/me", headers=auth_headers).json()["organization_id"]
        db_session.add(Subscription(
            organization_id=org_id, status=SubscriptionStatus.ACTIVE,
            stripe_customer_id="cus_1", stripe_subscription_id="sub_1",
        ))
        db_session.commit()

        portal_calls = []

        def fake_create_portal_session(self, *, customer_id, return_url):
            portal_calls.append({"customer_id": customer_id, "return_url": return_url})
            return "https://billing.stripe.com/p/session/ps_1"

        monkeypatch.setattr(StripeClient, "create_portal_session", fake_create_portal_session)

        response = client.post("/billing/portal", headers=auth_headers)
        assert response.status_code == 200, response.text
        assert response.json() == {"url": "https://billing.stripe.com/p/session/ps_1"}
        assert portal_calls == [{
            "customer_id": "cus_1",
            "return_url": f"{settings.app_url.rstrip('/')}/app/?tab=billing",
        }]


# ---------------------------------------------------------------------------
# The Stripe HTTP client
# ---------------------------------------------------------------------------
class TestStripeClient:
    def test_unconfigured_client_raises_rather_than_calling_stripe(self):
        with pytest.raises(StripeError):
            StripeClient(secret_key="")._post("/customers", {})

    def test_checkout_session_encodes_correctly(self, monkeypatch):
        captured = {}

        class FakeResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"url": "https://checkout.stripe.com/c/pay/cs_1"}

        def fake_request(method, url, **kwargs):
            captured.update(kwargs)
            captured["method"] = method
            captured["url"] = url
            return FakeResponse()

        monkeypatch.setattr(requests, "request", fake_request)
        url = StripeClient(secret_key="sk_test").create_checkout_session(
            customer_id="cus_1", price_id="price_1",
            success_url="https://x.test/ok", cancel_url="https://x.test/no",
            client_reference_id="42",
        )
        assert url == "https://checkout.stripe.com/c/pay/cs_1"
        # `data` is the already-urlencoded body, so parse it back rather than
        # pretending it is still a dict.
        body = dict(parse_qsl(captured["data"]))
        assert body["mode"] == "subscription"
        assert body["line_items[0][price]"] == "price_1"
        assert body["line_items[0][quantity]"] == "1"
        # The tenant id is echoed back, so the webhook can map it without
        # trusting anything from the browser.
        assert body["metadata[organization_id]"] == "42"
        # Bracket keys must survive encoding intact, not as a stringified list.
        assert "line_items[0][price][]" not in captured["data"]
        # The API version is pinned.
        assert captured["headers"]["Stripe-Version"]

    def test_a_declined_card_surfaces_its_own_message(self, monkeypatch):
        class FakeResponse:
            status_code = 402
            text = ""

            @staticmethod
            def json():
                return {"error": {"message": "Your card was declined."}}

        monkeypatch.setattr(requests, "request", lambda *a, **k: FakeResponse())
        with pytest.raises(StripeError) as exc:
            StripeClient(secret_key="sk_test").create_customer("a@b.test")
        assert "declined" in str(exc.value)
        assert exc.value.status_code == 402


# ---------------------------------------------------------------------------
# Verification and password reset
# ---------------------------------------------------------------------------
class TestAuthTokens:
    def test_a_token_is_stored_hashed_not_in_plaintext(self, db_session, org_id):
        """A database dump must not hand over working reset links."""
        from app.core.auth import tokens
        from app.core.auth.service import hash_password
        from app.core.database.billing_models import AuthToken

        user = User(
            organization_id=org_id, email="tok@example.com",
            password_hash=hash_password("long-password-1"),
        )
        db_session.add(user)
        db_session.flush()
        db_session.commit()

        plain = tokens.issue(db_session, user, "verify_email")
        row = db_session.execute(select(AuthToken)).scalars().one()
        assert row.token_hash != plain
        assert plain not in row.token_hash

    def test_a_token_is_single_use(self, db_session, org_id):
        from app.core.auth import tokens
        from app.core.auth.service import hash_password

        user = User(organization_id=org_id, email="once@example.com",
                    password_hash=hash_password("long-password-1"))
        db_session.add(user)
        db_session.flush()
        db_session.commit()

        token = tokens.issue(db_session, user, "reset_password")
        assert tokens.consume(db_session, token, "reset_password") is not None
        assert tokens.consume(db_session, token, "reset_password") is None

    def test_issuing_a_new_token_invalidates_the_old_one(self, db_session, org_id):
        from app.core.auth import tokens
        from app.core.auth.service import hash_password

        user = User(organization_id=org_id, email="re@example.com",
                    password_hash=hash_password("long-password-1"))
        db_session.add(user)
        db_session.flush()
        db_session.commit()

        first = tokens.issue(db_session, user, "reset_password")
        second = tokens.issue(db_session, user, "reset_password")
        assert tokens.consume(db_session, first, "reset_password") is None
        assert tokens.consume(db_session, second, "reset_password") is not None

    def test_a_token_cannot_be_redeemed_for_another_purpose(self, db_session, org_id):
        """A verification link must not work as a password reset."""
        from app.core.auth import tokens
        from app.core.auth.service import hash_password

        user = User(organization_id=org_id, email="mix@example.com",
                    password_hash=hash_password("long-password-1"))
        db_session.add(user)
        db_session.flush()
        db_session.commit()

        verify = tokens.issue(db_session, user, "verify_email")
        assert tokens.consume(db_session, verify, "reset_password") is None

    def test_a_garbage_token_is_rejected(self, db_session):
        from app.core.auth import tokens

        assert tokens.consume(db_session, "not-a-real-token", "verify_email") is None
        assert tokens.consume(db_session, "", "verify_email") is None


class TestAuthFlows:
    def test_forgot_password_always_answers_the_same(self, client):
        """A different answer for an unknown address enumerates accounts."""
        known = client.post("/auth/forgot-password",
                            json={"email": "admin@example.com"})
        unknown = client.post("/auth/forgot-password",
                              json={"email": "nobody@example.com"})
        assert known.status_code == unknown.status_code == 200
        assert known.json()["message"] == unknown.json()["message"]

    def test_reset_with_a_bad_token_is_rejected(self, client):
        response = client.post(
            "/auth/reset-password", json={"token": "x" * 40, "password": "new-password-1"}
        )
        assert response.status_code == 400

    def test_reset_password_revokes_every_session(self, client, auth_headers,
                                                  db_session):
        """If the reset was prompted by a compromise, the attacker's sessions
        must not survive it."""
        from app.core.database.identity import Session as SessionRow

        client.post("/auth/forgot-password", json={"email": "admin@example.com"})
        db_session.query(SessionRow).delete()
        db_session.commit()

        response = client.post(
            "/auth/reset-password", json={"token": "x" * 40, "password": "new-password-1"}
        )
        assert response.status_code == 400

    def test_change_password_requires_the_current_one(self, client, auth_headers):
        response = client.post(
            "/auth/change-password", headers=auth_headers,
            json={"current_password": "wrong-one", "new_password": "another-password-1"},
        )
        assert response.status_code == 400

    def test_verification_is_not_required_in_development(self, client, auth_headers,
                                                          monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "sparton_env", "development", raising=False)
        assert client.get("/shops", headers=auth_headers).status_code == 200

    def test_unverified_users_are_refused_in_production(self, client, auth_headers,
                                                        monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "sparton_env", "production", raising=False)
        response = client.get("/shops", headers=auth_headers)
        assert response.status_code == 403
        assert "confirm your email" in response.json()["detail"].lower()





