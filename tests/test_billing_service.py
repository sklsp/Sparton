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

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.billing.plans import BUSINESS, DEFAULT_PLAN, FREE, PRO
from app.billing.service import (
    _as_datetime,
    _follow_plan_cadence,
    _live_subscription,
    _record_invoice,
    _resolve_organization,
    already_processed,
    effective_plan,
    get_subscription,
    handle_event,
    mark_processed,
    plan_for_price,
)
from app.core.database.billing_models import Invoice, StripeEvent, Subscription, SubscriptionStatus
from app.core.database.ecommerce_models import Shop
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


class FakeStripeClient:
    """A stand-in for StripeClient; maps subscription ids to canned objects.

    An id mapped to an Exception makes ``get_subscription`` raise it, which is
    how the fetch-failure paths are exercised without touching the network.
    """

    def __init__(self, by_id=None):
        self.by_id = by_id or {}
        self.calls: list[str] = []

    def get_subscription(self, subscription_id):
        self.calls.append(subscription_id)
        response = self.by_id.get(subscription_id)
        if isinstance(response, Exception):
            raise response
        return response or {}


def make_event(event_id: str, event_type: str, obj: dict) -> dict:
    """The shape of a Stripe webhook payload the service consumes."""
    return {"id": event_id, "type": event_type, "data": {"object": obj}}


def sub_object(subscription_id="sub_1", price_id=None, status="active") -> dict:
    items = [{"price": {"id": price_id}}] if price_id else []
    return {
        "id": subscription_id,
        "status": status,
        "items": {"data": items},
        "customer": None,
    }


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


# ---------------------------------------------------------------------------
# handle_event: checkout.session.completed
# ---------------------------------------------------------------------------
class TestHandleEventCheckout:
    def test_completed_grants_the_plan_from_the_live_subscription(
        self, db_session, org, stripe_prices
    ):
        # The checkout session carries a thin subscription stub; the service
        # must fetch the real one rather than trust it.
        client = FakeStripeClient({"sub_42": sub_object("sub_42", "price_pro_123")})
        obj = {"metadata": {"organization_id": str(org.id)}, "subscription": "sub_42"}
        outcome = handle_event(
            db_session, make_event("evt_co_1", "checkout.session.completed", obj), client
        )
        assert outcome == "checkout_completed"
        assert client.calls == ["sub_42"]
        row = get_subscription(db_session, org.id)
        assert (row.plan, row.status, row.stripe_subscription_id) == (
            PRO,
            SubscriptionStatus.ACTIVE,
            "sub_42",
        )
        assert org.plan == PRO

    def test_a_dict_subscription_is_unwrapped(self, db_session, org, stripe_prices):
        client = FakeStripeClient({"sub_43": sub_object("sub_43", "price_business_123")})
        obj = {
            "metadata": {"organization_id": str(org.id)},
            "subscription": {"id": "sub_43"},
        }
        outcome = handle_event(
            db_session, make_event("evt_co_2", "checkout.session.completed", obj), client
        )
        assert outcome == "checkout_completed"
        row = get_subscription(db_session, org.id)
        assert (row.plan, row.stripe_subscription_id) == (BUSINESS, "sub_43")

    def test_a_fetch_failure_falls_back_to_the_event_copy(
        self, db_session, org, stripe_prices
    ):
        client = FakeStripeClient({"sub_44": RuntimeError("stripe down")})
        obj = {
            "metadata": {"organization_id": str(org.id)},
            "subscription": "sub_44",
            "status": "active",
            "items": {"data": [{"price": {"id": "price_pro_123"}}]},
        }
        outcome = handle_event(
            db_session, make_event("evt_co_3", "checkout.session.completed", obj), client
        )
        assert outcome == "checkout_completed"
        row = get_subscription(db_session, org.id)
        assert (row.plan, row.status) == (PRO, SubscriptionStatus.ACTIVE)

    def test_without_a_subscription_it_is_unmapped(self, db_session, org):
        obj = {"metadata": {"organization_id": str(org.id)}}
        outcome = handle_event(
            db_session, make_event("evt_co_4", "checkout.session.completed", obj)
        )
        assert outcome == "checkout_unmapped"
        assert get_subscription(db_session, org.id) is None
        # Even an unmapped event is recorded so a retry cannot re-trigger it.
        assert already_processed(db_session, "evt_co_4") is True

    def test_for_an_unknown_org_it_is_unmapped(self, db_session):
        obj = {"subscription": "sub_45"}
        outcome = handle_event(
            db_session, make_event("evt_co_5", "checkout.session.completed", obj)
        )
        assert outcome == "checkout_unmapped"


# ---------------------------------------------------------------------------
# handle_event: customer.subscription.* events
# ---------------------------------------------------------------------------
class TestHandleEventSubscription:
    def test_created_grants_the_plan(self, db_session, org, stripe_prices):
        client = FakeStripeClient({"sub_1": sub_object("sub_1", "price_business_123")})
        obj = {"id": "sub_1", "metadata": {"organization_id": str(org.id)}, "status": "active"}
        outcome = handle_event(
            db_session, make_event("evt_sub_c", "customer.subscription.created", obj), client
        )
        assert outcome == "created"
        row = get_subscription(db_session, org.id)
        assert (row.plan, row.status) == (BUSINESS, SubscriptionStatus.ACTIVE)

    def test_updated_applies_the_live_copy_not_the_stale_event(
        self, db_session, org, stripe_prices
    ):
        # The event says Pro; the live subscription is Business. The fetch wins:
        # applying a stale copy could roll a customer back to an old plan.
        client = FakeStripeClient({"sub_2": sub_object("sub_2", "price_business_123")})
        obj = {
            "id": "sub_2",
            "metadata": {"organization_id": str(org.id)},
            "status": "active",
            "items": {"data": [{"price": {"id": "price_pro_123"}}]},
        }
        outcome = handle_event(
            db_session, make_event("evt_sub_u", "customer.subscription.updated", obj), client
        )
        assert outcome == "updated"
        row = get_subscription(db_session, org.id)
        assert row.plan == BUSINESS

    def test_deleted_downgrades_to_free(self, db_session, org, stripe_prices):
        client = FakeStripeClient({"sub_3": sub_object("sub_3", "price_pro_123")})
        obj = {"id": "sub_3", "metadata": {"organization_id": str(org.id)}}
        handle_event(
            db_session, make_event("evt_sub_d0", "customer.subscription.created", obj), client
        )
        outcome = handle_event(
            db_session, make_event("evt_sub_d1", "customer.subscription.deleted", obj), client
        )
        assert outcome == "subscription_deleted"
        row = get_subscription(db_session, org.id)
        assert (row.plan, row.status) == (FREE, SubscriptionStatus.CANCELED)
        assert org.plan == FREE

    def test_an_unmapped_subscription_event_is_ignored(self, db_session):
        obj = {"id": "sub_x"}  # no metadata, no customer we know
        outcome = handle_event(
            db_session, make_event("evt_sub_u2", "customer.subscription.updated", obj)
        )
        assert outcome == "unmapped"

    def test_a_stale_event_cannot_revive_a_canceled_subscription(
        self, db_session, org, stripe_prices
    ):
        client = FakeStripeClient({"sub_4": sub_object("sub_4", "price_pro_123")})
        obj = {"id": "sub_4", "metadata": {"organization_id": str(org.id)}}
        handle_event(
            db_session, make_event("evt_sub_s0", "customer.subscription.created", obj), client
        )
        handle_event(
            db_session, make_event("evt_sub_s1", "customer.subscription.deleted", obj), client
        )
        # A late retry of the old active state must not re-grant Pro.
        outcome = handle_event(
            db_session, make_event("evt_sub_s2", "customer.subscription.updated", obj), client
        )
        assert outcome == "updated"
        row = get_subscription(db_session, org.id)
        assert (row.plan, row.status) == (FREE, SubscriptionStatus.CANCELED)

    def test_an_ended_subscription_cannot_overwrite_a_newer_one(
        self, db_session, org, stripe_prices
    ):
        client = FakeStripeClient({
            "sub_new": sub_object("sub_new", "price_business_123"),
            "sub_old": sub_object("sub_old", None, status="canceled"),
        })
        obj = {"id": "sub_new", "metadata": {"organization_id": str(org.id)}}
        handle_event(
            db_session, make_event("evt_sub_n0", "customer.subscription.created", obj), client
        )
        # A late event about an older subscription that has ended.
        outcome = handle_event(
            db_session,
            make_event(
                "evt_sub_n1",
                "customer.subscription.updated",
                {"id": "sub_old", "metadata": {"organization_id": str(org.id)}},
            ),
            client,
        )
        assert outcome == "updated"
        row = get_subscription(db_session, org.id)
        assert (row.plan, row.status, row.stripe_subscription_id) == (
            BUSINESS,
            SubscriptionStatus.ACTIVE,
            "sub_new",
        )


# ---------------------------------------------------------------------------
# handle_event: invoices and edge cases
# ---------------------------------------------------------------------------
class TestHandleEventInvoicesAndEdgeCases:
    def test_invoice_paid_records_the_invoice(self, db_session, org):
        obj = {
            "id": "in_1",
            "amount_paid": 2900,
            "currency": "eur",
            "status": "paid",
            "metadata": {"organization_id": str(org.id)},
        }
        outcome = handle_event(db_session, make_event("evt_inv_p", "invoice.paid", obj))
        assert outcome == "paid"
        row = db_session.execute(select(Invoice)).scalars().one()
        assert (row.organization_id, row.stripe_invoice_id, row.amount_cents) == (
            org.id,
            "in_1",
            2900,
        )

    def test_payment_failed_without_an_org_still_records_the_invoice(self, db_session):
        obj = {"id": "in_2", "amount_due": 7900}
        outcome = handle_event(
            db_session, make_event("evt_inv_f", "invoice.payment_failed", obj)
        )
        # The audit string is the last dot segment of the event type.
        assert outcome == "payment_failed"
        row = db_session.execute(select(Invoice)).scalars().one()
        # No organization to attach it to, but the payment failure is still on record.
        assert (row.organization_id, row.amount_cents) == (None, 7900)

    def test_an_unknown_event_type_is_acknowledged_and_dropped(self, db_session):
        outcome = handle_event(
            db_session, make_event("evt_unk", "customer.created", {"id": "cus_x"})
        )
        assert outcome == "ignored"
        # Acknowledged on purpose: an error would make Stripe retry it forever.
        assert already_processed(db_session, "evt_unk") is True

    def test_a_missing_event_id_is_rejected(self, db_session):
        event = {"type": "invoice.paid", "data": {"object": {"id": "in_x"}}}
        outcome = handle_event(db_session, event)
        assert outcome == "missing_event_id"
        assert already_processed(db_session, "") is False

    def test_a_duplicate_event_id_is_skipped(self, db_session, org, stripe_prices):
        client = FakeStripeClient({"sub_5": sub_object("sub_5", "price_pro_123")})
        obj = {"id": "sub_5", "metadata": {"organization_id": str(org.id)}}
        handle_event(
            db_session, make_event("evt_dup_h", "customer.subscription.created", obj), client
        )
        # Stripe retries the same id with a different payload; it must be a no-op.
        outcome = handle_event(
            db_session,
            make_event("evt_dup_h", "customer.subscription.deleted", {"id": "sub_5"}),
            client,
        )
        assert outcome == "duplicate"
        row = get_subscription(db_session, org.id)
        assert (row.plan, row.status) == (PRO, SubscriptionStatus.ACTIVE)

    def test_a_failed_event_is_not_marked_processed(
        self, db_session, org, monkeypatch
    ):
        import app.billing.service as service_module

        def boom(*args, **kwargs):
            raise RuntimeError("database exploded")

        monkeypatch.setattr(service_module, "apply_subscription", boom)
        client = FakeStripeClient({"sub_6": sub_object("sub_6", "price_pro_123")})
        obj = {"id": "sub_6", "metadata": {"organization_id": str(org.id)}}
        with pytest.raises(RuntimeError, match="database exploded"):
            handle_event(
                db_session, make_event("evt_fail", "customer.subscription.updated", obj), client
            )
        # Not marked: Stripe will retry and the plan must not silently diverge.
        assert already_processed(db_session, "evt_fail") is False


# ---------------------------------------------------------------------------
# _live_subscription
# ---------------------------------------------------------------------------
class TestLiveSubscription:
    def test_with_a_client_it_returns_the_fetched_copy(self):
        live = {"id": "sub_9", "status": "past_due"}
        client = FakeStripeClient({"sub_9": live})
        assert _live_subscription({"id": "sub_9"}, client) is live

    def test_without_a_client_it_builds_one(self, monkeypatch):
        import app.billing.service as service_module

        live = {"id": "sub_10", "status": "active"}

        class Factory:
            @staticmethod
            def get_subscription(subscription_id):
                return live

        monkeypatch.setattr(service_module, "StripeClient", Factory)
        assert _live_subscription({"id": "sub_10"}, None) is live

    def test_when_the_fetch_fails_the_event_copy_is_used(self):
        obj = {"id": "sub_11", "status": "active"}
        client = FakeStripeClient({"sub_11": RuntimeError("stripe down")})
        assert _live_subscription(obj, client) is obj

    def test_without_an_id_nothing_is_fetched(self):
        client = FakeStripeClient()
        obj = {"status": "active"}
        result = _live_subscription(obj, client)
        assert result is obj and client.calls == []


# ---------------------------------------------------------------------------
# _follow_plan_cadence
# ---------------------------------------------------------------------------
def make_shop(db_session, org, domain="example.com", last_crawled_at=None):
    shop = Shop(
        organization_id=org.id, name=f"Shop {domain}", url=f"https://{domain}", domain=domain
    )
    if last_crawled_at is not None:
        shop.last_crawled_at = last_crawled_at
    db_session.add(shop)
    db_session.flush()
    return shop


class TestFollowPlanCadence:
    def test_an_upgrade_sets_the_pro_cadence_on_existing_shops(self, db_session, org):
        crawled_at = datetime(2024, 5, 1, 9, 0)
        fresh = make_shop(db_session, org, "fresh.example.com")
        active = make_shop(
            db_session, org, "active.example.com", last_crawled_at=crawled_at
        )

        org.plan = PRO
        _follow_plan_cadence(db_session, org)
        db_session.commit()

        assert fresh.crawl_frequency_hours == 24
        # A shop that has never been crawled gets no new due date.
        assert fresh.next_crawl_at is None
        assert active.crawl_frequency_hours == 24
        # Due right away: last crawl plus the new, faster interval.
        assert active.next_crawl_at == crawled_at + timedelta(hours=24)

    def test_a_downgrade_restores_the_weekly_cadence(self, db_session, org):
        shop = make_shop(db_session, org)
        org.plan = FREE
        _follow_plan_cadence(db_session, org)
        db_session.commit()
        assert shop.crawl_frequency_hours == 168


# ---------------------------------------------------------------------------
# _record_invoice
# ---------------------------------------------------------------------------
class TestRecordInvoice:
    def test_with_an_organization(self, db_session, org):
        _record_invoice(db_session, org, {"id": "in_9", "amount_paid": 500})
        row = db_session.execute(select(Invoice)).scalars().one()
        assert (row.organization_id, row.amount_cents) == (org.id, 500)

    def test_without_an_organization(self, db_session):
        _record_invoice(db_session, None, {"id": "in_10", "amount_due": 700})
        row = db_session.execute(select(Invoice)).scalars().one()
        assert (row.organization_id, row.amount_cents) == (None, 700)

    def test_an_invoice_is_recorded_once(self, db_session, org):
        _record_invoice(db_session, org, {"id": "in_11", "amount_paid": 500})
        # Invoices are immutable: a repeat must not create or change anything.
        _record_invoice(db_session, org, {"id": "in_11", "amount_paid": 999})
        rows = db_session.execute(select(Invoice)).scalars().all()
        assert len(rows) == 1 and rows[0].amount_cents == 500

    def test_a_missing_id_records_nothing(self, db_session, org):
        _record_invoice(db_session, org, {"amount_paid": 500})
        assert db_session.execute(select(Invoice)).scalars().all() == []

