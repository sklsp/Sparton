"""Billing state: subscriptions, processed Stripe events, invoices.

The important row here is ``Subscription`` because it is the **only** thing
that grants a paid plan. It is written *exclusively* by the Stripe webhook
handler (D-014): the post-checkout redirect is a UX affordance, not a payment
signal, and a customer who closes the tab must not be left paying for nothing
— nor must a customer who closes the tab be able to keep Pro for free.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, JSONType
from app.core.database.models import utcnow


class SubscriptionStatus:
    ACTIVE = "active"
    TRIALING = "trialing"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    UNPAID = "unpaid"

    #: Statuses that grant a paid plan. `past_due` is included deliberately:
    #: a card that failed once should not lock a customer out of their own data
    #: mid-week, and Stripe will keep retrying.
    GRANTING = frozenset({ACTIVE, TRIALING, PAST_DUE})


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        # One live subscription row per organization; history lives in Stripe.
        UniqueConstraint("organization_id", name="uq_subscription_org"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )

    #: Our plan key: "free" | "pro" | "business" (app/billing/plans.py).
    plan: Mapped[str] = mapped_column(String(24), default="free", index=True)
    status: Mapped[str] = mapped_column(
        String(24), default=SubscriptionStatus.ACTIVE, index=True
    )

    #: Stripe identifiers, for reconciliation.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(80), index=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(
        String(80), unique=True, index=True
    )
    stripe_price_id: Mapped[str | None] = mapped_column(String(80))

    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def grants_plan(self) -> bool:
        return self.status in SubscriptionStatus.GRANTING

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plan": self.plan,
            "status": self.status,
            "stripe_customer_id": self.stripe_customer_id,
            "current_period_end": (
                self.current_period_end.isoformat() if self.current_period_end else None
            ),
            "cancel_at_period_end": self.cancel_at_period_end,
        }


class StripeEvent(Base):
    """A processed Stripe event id.

    Stripe delivers webhooks **at least once**. Without this table a retry
    re-applies a subscription change, and a customer who paid for two months
    can end up on the wrong plan. The unique id is the idempotency key.
    """

    __tablename__ = "stripe_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Stripe's `evt_...` id.
    event_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class Invoice(Base):
    """A billing event worth showing the customer in their billing history."""

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    stripe_invoice_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    #: "paid" | "open" | "void" | "uncollectible"
    status: Mapped[str] = mapped_column(String(24), default="", index=True)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="eur")
    hosted_invoice_url: Mapped[str] = mapped_column(Text, default="")
    invoice_pdf: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


Index("ix_billing_event_type_time", StripeEvent.event_type, StripeEvent.processed_at)


# --------------------------------------------------------------------------
# Auth tokens
# --------------------------------------------------------------------------
class AuthTokenPurpose:
    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"

    ALL = (VERIFY_EMAIL, RESET_PASSWORD)


class AuthToken(Base):
    """A one-time token for email verification or password reset.

    One table for both purposes, discriminated by ``purpose`` (D-011): two
    near-identical tables is two places to get the hashing wrong.

    The token is stored **hashed**, exactly like a session token, so a database
    dump does not hand an attacker working reset links.
    """

    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    #: "verify_email" | "reset_password"
    purpose: Mapped[str] = mapped_column(String(24), index=True)
    #: SHA-256 of the token. The plaintext only ever exists in the email.
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    @property
    def is_usable(self) -> bool:
        if self.used_at is not None:
            return False
        expires = self.expires_at
        if expires.tzinfo is None:
            from datetime import timezone

            expires = expires.replace(tzinfo=timezone.utc)
        return expires > utcnow()


__all__ = [
    "AuthToken",
    "AuthTokenPurpose",
    "Invoice",
    "StripeEvent",
    "Subscription",
    "SubscriptionStatus",
]

