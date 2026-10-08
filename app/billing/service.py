"""Subscription state — the only writer of paid plans.

The rule this module exists to enforce (D-014): **a Stripe webhook is the only
thing that grants a paid plan.** The post-checkout redirect is a UX affordance
that says "thanks", never "you are now on Pro". A customer who closes the tab
must not lose their upgrade, and a customer who closes the tab must not keep
Pro for free.

Two more properties worth stating:

* **Idempotent.** Stripe delivers at least once. Every event id is recorded
  in ``stripe_events``; a repeat is acknowledged and ignored rather than
  re-applied, which would otherwise let a retry move a customer to the wrong
  plan.
* **Never trust the browser.** The organization is resolved from the event's
  metadata or the Stripe customer id, both of which we wrote ourselves.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing.plans import BUSINESS, DEFAULT_PLAN, FREE, PRO, check_crawl_frequency, get_plan
from app.billing.stripe import StripeClient
from app.core.config import settings
from app.core.database.billing_models import (
    Invoice,
    StripeEvent,
    Subscription,
    SubscriptionStatus,
)
from app.core.database.identity import Organization
from app.core.database.models import utcnow

logger = logging.getLogger(__name__)

#: Events we act on. Everything else is acknowledged and dropped.
HANDLED_EVENTS = frozenset({
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
})


def plan_for_price(price_id: str | None) -> str:
    """Map a Stripe price id back to one of our plan keys.

    Reads the configured price ids rather than hardcoding them, so rotating a
    price in the Stripe dashboard needs no deploy. An unrecognised price fails
    closed to Free: better that a customer drops to the free allowance than
    that a typo grants unlimited access.
    """
    if not price_id:
        return DEFAULT_PLAN
    if price_id == (settings.stripe_price_pro or ""):
        return PRO
    if price_id == (settings.stripe_price_business or ""):
        return BUSINESS
    logger.warning("Unrecognised Stripe price id %r; defaulting to Free", price_id)
    return FREE


def _as_datetime(value: Any) -> datetime | None:
    """Stripe sends unix timestamps; occasionally ISO strings. Accept both."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(int(value))
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def get_subscription(db: Session, organization_id: int) -> Subscription | None:
    return db.execute(
        select(Subscription).where(Subscription.organization_id == organization_id)
    ).scalars().first()


def effective_plan(db: Session, organization_id: int | None) -> str:
    """The plan actually in force for an organization.

    The subscription row is authoritative. The organization's own ``plan``
    column is a denormalised copy used for cheap reads, and is only trusted
    when there is no subscription row at all (a self-serve free account).
    """
    if organization_id is None:
        return DEFAULT_PLAN
    subscription = get_subscription(db, organization_id)
    if subscription is not None:
        # A cancelled or unpaid subscription grants nothing: the webhook has
        # already downgraded the organization, so read it from there.
        return subscription.plan if subscription.grants_plan else DEFAULT_PLAN
    org = db.get(Organization, organization_id)
    return (org.plan if org else DEFAULT_PLAN) or DEFAULT_PLAN


def already_processed(db: Session, event_id: str) -> bool:
    return db.execute(
        select(StripeEvent.id).where(StripeEvent.event_id == event_id)
    ).first() is not None


def mark_processed(db: Session, event_id: str, event_type: str) -> None:
    db.add(StripeEvent(event_id=event_id, event_type=event_type))
    db.commit()


def _resolve_organization(
    db: Session, obj: dict[str, Any], client_reference: str = ""
) -> Organization | None:
    """Find the tenant a Stripe object belongs to.

    Three routes, in order of trust: explicit metadata we set ourselves, the
    customer id we recorded, then the client reference. Never a browser value.
    """
    metadata = obj.get("metadata") or {}
    org_id = metadata.get("organization_id")
    if org_id and str(org_id).isdigit():
        org = db.get(Organization, int(org_id))
        if org is not None:
            return org

    customer_id = obj.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    if customer_id:
        row = db.execute(
            select(Subscription).where(
                Subscription.stripe_customer_id == str(customer_id)
            )
        ).scalars().first()
        if row is not None:
            return db.get(Organization, row.organization_id)

    if client_reference and str(client_reference).isdigit():
        return db.get(Organization, int(client_reference))
    return None


def apply_subscription(
    db: Session,
    org: Organization,
    subscription: dict[str, Any],
    *,
    deleted: bool = False,
) -> Subscription:
    """Create or update the subscription row from a Stripe subscription object."""
    items = (subscription.get("items") or {}).get("data") or []
    price_id = items[0].get("price", {}).get("id") if items else None
    status = str(subscription.get("status") or "").lower()
    if deleted:
        status = SubscriptionStatus.CANCELED

    plan = DEFAULT_PLAN if deleted else plan_for_price(price_id)
    customer_id = subscription.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")

    row = get_subscription(db, org.id)
    if row is None:
        row = Subscription(organization_id=org.id)
        db.add(row)

    row.plan = plan
    row.status = status
    if customer_id:
        row.stripe_customer_id = str(customer_id)
    row.stripe_subscription_id = str(subscription.get("id") or "") or None
    row.stripe_price_id = price_id
    row.current_period_start = _as_datetime(subscription.get("current_period_start"))
    row.current_period_end = _as_datetime(subscription.get("current_period_end"))
    row.cancel_at_period_end = bool(subscription.get("cancel_at_period_end"))

    # Keep the organization's denormalised copy in step, so a free-plan read
    # does not need to join the subscription table.
    previous_plan = org.plan
    org.plan = plan if row.grants_plan else DEFAULT_PLAN
    org.subscription_ends_at = row.current_period_end
    if org.plan != previous_plan:
        _follow_plan_cadence(db, org)
    db.commit()
    db.refresh(row)

    from app.core.auth.service import audit

    audit(
        db,
        action="billing.subscription_updated",
        organization_id=org.id,
        resource=f"subscription:{row.stripe_subscription_id or 'new'}",
        detail={"plan": plan, "status": status},
    )
    logger.info("Subscription for org %s: plan=%s status=%s", org.id, plan, status)
    return row


def _follow_plan_cadence(db: Session, org: Organization) -> None:
    """A plan change applies to the shops that exist, not only to new ones.

    Without this, a shop added on Free stayed weekly after an upgrade to Pro,
    and a cancelled Pro kept its daily checks. A slower cadence set through
    the API is replaced too; the dashboard offers no such setting.
    """
    from datetime import timedelta

    from app.core.database.ecommerce_models import Shop

    interval = check_crawl_frequency(org.plan, None)
    for shop in db.execute(select(Shop).where(Shop.organization_id == org.id)).scalars():
        shop.crawl_frequency_hours = interval
        if shop.last_crawled_at is not None:
            # An upgrade can make a shop due right away; a downgrade pushes it out.
            shop.next_crawl_at = shop.last_crawled_at + timedelta(hours=interval)


def _record_invoice(db: Session, org: Organization | None, invoice: dict[str, Any]) -> None:
    invoice_id = str(invoice.get("id") or "")
    if not invoice_id:
        return
    existing = db.execute(
        select(Invoice).where(Invoice.stripe_invoice_id == invoice_id)
    ).scalars().first()
    if existing is not None:
        return  # invoices are immutable once created
    amount = invoice.get("amount_paid") or invoice.get("amount_due") or 0
    db.add(
        Invoice(
            organization_id=org.id if org else None,
            stripe_invoice_id=invoice_id,
            status=str(invoice.get("status") or ""),
            amount_cents=int(amount),
            currency=str(invoice.get("currency") or "eur"),
            hosted_invoice_url=str(invoice.get("hosted_invoice_url") or ""),
            invoice_pdf=str(invoice.get("invoice_pdf") or ""),
        )
    )
    db.commit()


def handle_event(db: Session, event: dict[str, Any], client: StripeClient | None = None) -> str:
    """Apply one Stripe event. Returns a short string for the audit log.

    Unknown event types are acknowledged and ignored on purpose: Stripe adds
    new event types regularly, and returning an error for one we do not handle
    makes Stripe retry it forever and show a red banner in the dashboard.
    """
    event_id = str(event.get("id") or "")
    event_type = str(event.get("type") or "")
    if not event_id:
        return "missing_event_id"

    # Stripe delivers at least once. A repeat must be a no-op, not a re-apply.
    if already_processed(db, event_id):
        logger.info("Ignoring already-processed Stripe event %s", event_id)
        return "duplicate"

    obj = event.get("data", {}).get("object") or {}
    outcome = "ignored"

    try:
        if event_type == "checkout.session.completed":
            org = _resolve_organization(db, obj, str(obj.get("client_reference_id") or ""))
            subscription_id = obj.get("subscription")
            if isinstance(subscription_id, dict):
                subscription_id = subscription_id.get("id")
            if org is not None and subscription_id:
                # `checkout.session` carries a thin subscription object, so
                # fetch the real one rather than trusting the stub.
                stripe = client or StripeClient()
                try:
                    subscription = stripe.get_subscription(str(subscription_id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not fetch subscription: %s", exc)
                    subscription = obj
                apply_subscription(db, org, subscription)
                outcome = "checkout_completed"
            else:
                outcome = "checkout_unmapped"

        elif event_type in (
            "customer.subscription.created",
            "customer.subscription.updated",
        ):
            org = _resolve_organization(db, obj)
            if org is not None:
                apply_subscription(db, org, obj)
                outcome = event_type.rsplit(".", 1)[-1]
            else:
                outcome = "unmapped"

        elif event_type == "customer.subscription.deleted":
            org = _resolve_organization(db, obj)
            if org is not None:
                apply_subscription(db, org, obj, deleted=True)
                outcome = "subscription_deleted"
            else:
                outcome = "unmapped"

        elif event_type in ("invoice.paid", "invoice.payment_failed"):
            org = _resolve_organization(db, obj)
            _record_invoice(db, org, obj)
            outcome = event_type.rsplit(".", 1)[-1]
    except Exception:
        # Do NOT mark as processed on failure: Stripe should retry, and the
        # customer's plan must not silently diverge from what they paid for.
        logger.exception("Failed to apply Stripe event %s (%s)", event_id, event_type)
        raise

    mark_processed(db, event_id, event_type)
    return outcome


