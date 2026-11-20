"""Unit tests for the helpers in app/billing/service.py.

The webhook handler is built on five small pieces of logic, and each one fails
in a security-relevant direction if it breaks: plan lookup must fail closed to
Free, timestamps from Stripe come in two shapes, the subscription row (not the
denormalised org column) decides what a customer gets, event ids are the
idempotency key, and tenant resolution must never trust a browser value.

Stripe itself is never called here: only rows in our own database and plain
values, so no fake client or HTTP transport is needed (unlike test_billing.py).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.billing.plans import BUSINESS, DEFAULT_PLAN, FREE, PRO
from app.billing.service import (
    _as_datetime,
    _resolve_organization,
    already_processed,
    effective_plan,
    mark_processed,
    plan_for_price,
)
from app.core.database.billing_models import StripeEvent, Subscription, SubscriptionStatus
from app.core.database.identity import Organization


def make_org(db_session, name: str) -> Organization:
    from app.core.auth.service import unique_slug

    org = Organization(name=name, slug=unique_slug(db_session, name))
    db_session.add(org)
    db_session.flush()
    return org


@pytest.fixture()
def org(db_session):
    """A fresh organization on the default free plan."""
    return make_org(db_session, "Service Test")


@pytest.fixture()
def stripe_prices(monkeypatch):
    """Point the plan mapping at known fake price ids."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "stripe_price_pro", "price_pro_123", raising=False)
    monkeypatch.setattr(settings, "stripe_price_business", "price_business_123", raising=False)
    return settings


# ---------------------------------------------------------------------------
# plan_for_price
# ---------------------------------------------------------------------------
class TestPlanForPrice:
    def test_each_configured_price_maps_to_its_plan(self, stripe_prices):
        assert plan_for_price("price_pro_123") == PRO
        assert plan_for_price("price_business_123") == BUSINESS

    def test_none_and_empty_fail_closed_to_free(self, stripe_prices):
        assert plan_for_price(None) == DEFAULT_PLAN
        assert plan_for_price("") == DEFAULT_PLAN

    def test_an_unknown_price_fails_closed_to_free(self, stripe_prices):
        # A typo in the dashboard must drop a customer to Free, never grant access.
        assert plan_for_price("price_typo_999") == FREE

    def test_with_no_configured_prices_everything_is_free(self):
        # settings.stripe_price_pro is None by default; the lookup must not crash or grant.
        assert plan_for_price("anything_at_all") == FREE


# ---------------------------------------------------------------------------
# _as_datetime
# ---------------------------------------------------------------------------
class TestAsDatetime:
    def test_unix_seconds(self):
        assert _as_datetime(1_700_000_000) == datetime.fromtimestamp(1_700_000_000)

    def test_float_timestamps_are_truncated_to_whole_seconds(self):
        assert _as_datetime(1_700_000_000.9) == datetime.fromtimestamp(1_700_000_000)

    def test_iso_string_with_z_suffix(self):
        assert _as_datetime("2024-01-15T10:30:00Z") == datetime(2024, 1, 15, 10, 30, tzinfo=UTC)

    def test_iso_string_with_explicit_offset(self):
        # 10:30 at +02:00 is the same instant as 08:30 UTC.
        assert _as_datetime("2024-01-15T10:30:00+02:00") == datetime(2024, 1, 15, 8, 30, tzinfo=UTC)

    def test_none_stays_none(self):
        assert _as_datetime(None) is None

    def test_junk_string_returns_none(self):
        assert _as_datetime("not-a-timestamp") is None

    def test_unexpected_types_return_none(self):
        assert _as_datetime({"ts": 1_700_000_000}) is None


# ---------------------------------------------------------------------------
# effective_plan
# ---------------------------------------------------------------------------
class TestEffectivePlan:
    def test_no_organization_is_free(self, db_session):
        assert effective_plan(db_session, None) == DEFAULT_PLAN

    def test_unknown_organization_id_is_free(self, db_session):
        assert effective_plan(db_session, 9_999_999) == FREE

    def test_without_a_subscription_the_org_column_is_trusted(self, db_session, org):
        # A self-serve free account has no subscription row at all; the denormalised
        # copy on the organization is then the only source.
        org.plan = BUSINESS
        db_session.commit()
        assert effective_plan(db_session, org.id) == BUSINESS

    def test_an_active_subscription_grants_its_plan(self, db_session, org):
        # The row wins over a stale denormalised copy on the organization.
        db_session.add(Subscription(organization_id=org.id, plan=BUSINESS, status=SubscriptionStatus.ACTIVE))
        db_session.commit()
        assert effective_plan(db_session, org.id) == BUSINESS

    def test_a_past_due_subscription_still_grants_its_plan(self, db_session, org):
        # past_due is deliberately in GRANTING: one failed card must not lock a customer
        # out of their own data mid-week while Stripe keeps retrying.
        db_session.add(Subscription(organization_id=org.id, plan=PRO, status=SubscriptionStatus.PAST_DUE))
        db_session.commit()
        assert effective_plan(db_session, org.id) == PRO

    def test_a_cancelled_subscription_grants_nothing(self, db_session, org):
        # The webhook has already downgraded the organization; a cancelled row grants Free.
        db_session.add(Subscription(organization_id=org.id, plan=BUSINESS, status=SubscriptionStatus.CANCELED))
        db_session.commit()
        assert effective_plan(db_session, org.id) == DEFAULT_PLAN


# ---------------------------------------------------------------------------
# already_processed / mark_processed
# ---------------------------------------------------------------------------
class TestEventIdempotency:
    def test_a_fresh_event_is_not_processed(self, db_session):
        assert already_processed(db_session, "evt_new") is False

    def test_mark_processed_records_the_id_and_type(self, db_session):
        mark_processed(db_session, "evt_1", "invoice.paid")
        row = db_session.execute(select(StripeEvent)).scalars().one()
        assert (row.event_id, row.event_type) == ("evt_1", "invoice.paid")

    def test_the_same_event_id_twice_is_seen_as_processed(self, db_session):
        # Stripe delivers at least once; the second sighting must be recognised as a repeat.
        mark_processed(db_session, "evt_dup", "customer.subscription.updated")
        assert already_processed(db_session, "evt_dup") is True

    def test_marking_the_same_id_twice_is_refused(self, db_session):
        # The unique event id is the idempotency key; a double write must not succeed.
        mark_processed(db_session, "evt_once", "invoice.paid")
        with pytest.raises(IntegrityError):
            mark_processed(db_session, "evt_once", "invoice.paid")
        db_session.rollback()

    def test_other_events_are_unaffected(self, db_session):
        mark_processed(db_session, "evt_a", "invoice.paid")
        assert already_processed(db_session, "evt_b") is False


# ---------------------------------------------------------------------------
# _resolve_organization
# ---------------------------------------------------------------------------
class TestResolveOrganization:
    def test_metadata_wins_when_the_org_exists(self, db_session, org):
        obj = {"metadata": {"organization_id": str(org.id)}}
        resolved = _resolve_organization(db_session, obj)
        assert resolved is not None and resolved.id == org.id

    def test_customer_string_finds_the_subscriber(self, db_session, org):
        db_session.add(Subscription(organization_id=org.id, stripe_customer_id="cus_1"))
        db_session.commit()
        resolved = _resolve_organization(db_session, {"customer": "cus_1"})
        assert resolved is not None and resolved.id == org.id

    def test_customer_as_nested_object(self, db_session, org):
        # Stripe sometimes expands the customer into an object; both shapes must work.
        db_session.add(Subscription(organization_id=org.id, stripe_customer_id="cus_2"))
        db_session.commit()
        resolved = _resolve_organization(db_session, {"customer": {"id": "cus_2"}})
        assert resolved is not None and resolved.id == org.id

    def test_client_reference_is_the_last_route(self, db_session, org):
        resolved = _resolve_organization(db_session, {}, client_reference=str(org.id))
        assert resolved is not None and resolved.id == org.id

    def test_metadata_beats_customer(self, db_session, org):
        other = make_org(db_session, "Other Tenant")
        db_session.add(Subscription(organization_id=other.id, stripe_customer_id="cus_3"))
        db_session.commit()
        obj = {"metadata": {"organization_id": str(org.id)}, "customer": "cus_3"}
        resolved = _resolve_organization(db_session, obj)
        assert resolved is not None and resolved.id == org.id

    def test_non_digit_metadata_falls_through_to_customer(self, db_session, org):
        # A non-numeric organization_id must be skipped, not crash the handler.
        db_session.add(Subscription(organization_id=org.id, stripe_customer_id="cus_4"))
        db_session.commit()
        obj = {"metadata": {"organization_id": "abc"}, "customer": "cus_4"}
        resolved = _resolve_organization(db_session, obj)
        assert resolved is not None and resolved.id == org.id

    def test_missing_org_in_metadata_falls_through_to_customer(self, db_session, org):
        # A numeric id with no matching row must fall through to the customer route.
        db_session.add(Subscription(organization_id=org.id, stripe_customer_id="cus_5"))
        db_session.commit()
        obj = {"metadata": {"organization_id": "99999"}, "customer": "cus_5"}
        resolved = _resolve_organization(db_session, obj)
        assert resolved is not None and resolved.id == org.id

    def test_nothing_matches_returns_none(self, db_session):
        assert _resolve_organization(db_session, {}) is None

    def test_non_digit_client_reference_is_ignored(self, db_session):
        # A browser-supplied reference that is not our numeric id must resolve to nothing.
        assert _resolve_organization(db_session, {}, client_reference="not-a-number") is None

