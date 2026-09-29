"""Billing API: plans, Checkout, Customer Portal, and the Stripe webhook.

    GET  /billing/plans         the pricing table (public, drives the landing page)
    GET  /billing/plan          the caller's plan and current usage
    GET  /billing/usage         token spend over the last N days
    POST /billing/checkout      start Stripe Checkout
    POST /billing/portal        open the Customer Portal (cancel / change card)
    POST /stripe/webhook        Stripe's only callback; signature-verified

`/stripe/webhook` is exempt from session auth — Stripe has no session — but
**not** from signature verification, body-size limits, or rate limiting.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.billing.plans import (
    BUSINESS,
    PLANS,
    PRO,
    all_plans,
    get_plan,
    usage_report,
)
from app.billing.service import effective_plan, get_subscription, handle_event
from app.billing.stripe import (
    StripeClient,
    StripeError,
    is_configured as stripe_configured,
    parse_event,
    verify_signature,
)
from app.core.auth.api import DbSession, current_user
from app.core.config import settings
from app.core.database.billing_models import Invoice
from app.core.database.identity import Organization
from app.core.database.usage_models import usage_summary
from app.core.security.rate_limit import rate_limit

router = APIRouter(tags=["billing"])

#: A Stripe event is a few kB. Anything larger is not one.
MAX_WEBHOOK_BYTES = 256 * 1024


class CheckoutRequest(BaseModel):
    plan: str = Field(pattern="^(pro|business)$")


def _org_id(user) -> int | None:
    return getattr(user, "organization_id", None)


def _price_id(plan: str) -> str:
    price = PLANS[plan].price_env
    return str(getattr(settings, price) or "") if price else ""


@router.get("/billing/plans")
def list_plans() -> dict:
    """The pricing table. Public: the landing page needs it before signup."""
    return {
        "plans": all_plans(),
        "trial_days": 14,
        "currency": "eur",
    }


@router.get("/billing/plan")
def current_plan(
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    """The caller's plan, their usage, and what they could still add."""
    org_id = _org_id(user)
    plan_id = effective_plan(db, org_id)
    payload = usage_report(db, org_id, plan_id)
    subscription = get_subscription(db, org_id) if org_id else None
    payload["subscription"] = subscription.to_dict() if subscription else None
    payload["billing_enabled"] = bool(settings.billing_enabled and stripe_configured())
    return payload


@router.get("/billing/usage")
def usage(
    days: int = 30,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    """Measured token spend. This is what `docs/LAUNCH.md` quotes as unit cost."""
    org_id = _org_id(user)
    if org_id is None:
        return {"days": days, "requests": 0, "total_tokens": 0,
                "estimated_cost_usd": 0.0, "by_task": {}, "by_model": {}}
    return usage_summary(db, org_id, days=max(1, min(days, 365)))


@router.get("/billing/invoices")
def invoices(
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    org_id = _org_id(user)
    rows = db.execute(
        select(Invoice)
        .where(Invoice.organization_id == org_id)
        .order_by(Invoice.created_at.desc())
        .limit(24)
    ).scalars().all()
    return {
        "count": len(rows),
        "invoices": [
            {
                "id": r.stripe_invoice_id,
                "status": r.status,
                "amount_cents": r.amount_cents,
                "currency": r.currency,
                "url": r.hosted_invoice_url,
                "pdf": r.invoice_pdf,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post("/billing/checkout")
def start_checkout(
    payload: CheckoutRequest,
    request: Request,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    """Send the customer to Stripe Checkout and return the URL.

    Nothing about the subscription is decided here. The plan is granted when
    the webhook arrives (D-014), so closing the tab loses nothing.
    """
    org_id = _org_id(user)
    if org_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No organization")
    if not settings.billing_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not enabled on this deployment",
        )
    if not stripe_configured():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing is not configured (STRIPE_SECRET_KEY is empty)",
        )

    price_id = _price_id(payload.plan)
    if not price_id:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No Stripe price is configured for the {payload.plan} plan",
        )

    org = db.get(Organization, org_id)
    subscription = get_subscription(db, org_id)
    customer_id = subscription.stripe_customer_id if subscription else ""
    client = StripeClient()

    if not customer_id:
        email = getattr(user, "email", "") or ""
        customer_id = client.create_customer(
            email, name=org.name if org else "", metadata={"organization_id": org_id}
        )
        if subscription is None:
            from app.core.database.billing_models import Subscription

            subscription = Subscription(organization_id=org_id)
            db.add(subscription)
        subscription.stripe_customer_id = customer_id
        db.commit()

    base = settings.app_url.rstrip("/")
    url = client.create_checkout_session(
        customer_id=customer_id,
        price_id=price_id,
        success_url=f"{base}/app/?checkout=success",
        cancel_url=f"{base}/app/?checkout=cancelled",
        client_reference_id=str(org_id),
    )
    return {"url": url, "plan": payload.plan}


@router.post("/billing/portal")
def open_portal(
    request: Request,
    db: DbSession = None,
    user: Annotated[object, Depends(current_user)] = None,
) -> dict:
    """Open the Customer Portal, where a customer cancels or swaps card."""
    org_id = _org_id(user)
    subscription = get_subscription(db, org_id) if org_id else None
    if subscription is None or not subscription.stripe_customer_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="This account has no subscription to manage",
        )
    base = settings.app_url.rstrip("/")
    url = StripeClient().create_portal_session(
        customer_id=subscription.stripe_customer_id,
        return_url=f"{base}/app/?tab=billing",
    )
    return {"url": url}


@router.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    db: DbSession = None,
    _rate: None = Depends(rate_limit(limit=120)),
) -> dict:
    """Stripe's only callback.

    No session auth — Stripe has no session — but every other control applies:
    a hard body-size cap, a constant-time HMAC over the **raw** body, and a
    rate limit. A missing webhook secret is a deployment error, and is refused
    loudly rather than defaulting to "accept everything".
    """
    if not settings.stripe_webhook_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Stripe webhooks are not configured (STRIPE_WEBHOOK_SECRET)",
        )

    body = await request.body()
    if len(body) > MAX_WEBHOOK_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Webhook body too large"
        )

    signature = request.headers.get("stripe-signature", "")
    if not verify_signature(body, signature, settings.stripe_webhook_secret):
        # 400, not 401: the request is malformed/unauthenticated, and Stripe
        # surfaces a non-2xx as a delivery failure worth investigating.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="Invalid Stripe signature"
        )

    try:
        event = parse_event(body)
    except StripeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=exc.message) from exc

    try:
        outcome = handle_event(db, event)
    except Exception as exc:  # noqa: BLE001
        # 500 so Stripe retries: a transient failure must not cost a customer
        # the plan they paid for.
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not apply the event: {exc}",
        ) from exc

    return {"received": True, "outcome": outcome}


__all__ = ["router"]

