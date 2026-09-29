"""Stripe: Checkout, Customer Portal, and signed webhooks.

Implemented directly against Stripe's HTTPS API with ``requests`` (already a
dependency) rather than adding the ``stripe`` package (D-002). The surface we
need is four calls and one HMAC:

* ``POST /v1/checkout/sessions``
* ``POST /v1/billing_portal/sessions``
* ``GET  /v1/subscriptions/{id}``
* ``POST /v1/customers``

plus webhook signature verification, which is ~30 lines of stdlib HMAC.

The consequences of that choice, stated plainly:

* The pinned `Stripe-Version` header means Stripe cannot change response shapes
  out from under us, which is the main argument for the official library.
* Form encoding is mandatory — Stripe's API is not JSON. Getting it wrong
  fails loudly and immediately, not subtly.
* Everything goes through one `StripeClient`, so swapping to the official
  library later is a single-file change.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

import requests

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Pinned so Stripe cannot change response shapes under us.
API_VERSION = "2024-06-20"
BASE_URL = "https://api.stripe.com/v1"

#: Webhook signatures older than this are rejected: a captured request must not
#: be replayable forever.
WEBHOOK_TOLERANCE_SECONDS = 300


class StripeError(RuntimeError):
    """Stripe rejected a request, or we could not reach it."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def is_configured() -> bool:
    return bool(settings.stripe_secret_key)


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts into Stripe's bracket notation.

    `{"a": {"b": 1}}` becomes `{"a[b]": 1}`. Keys that already use brackets
    (our `line_items[0][price]` style) pass through untouched.
    """
    flat: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict) and "[" not in str(key):
            flat.update(_flatten(value, name))
        else:
            flat[name] = value
    return flat


def _error_message(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:200]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(payload)[:300]


class StripeClient:
    """A very small Stripe client. Every call raises `StripeError` on failure."""

    def __init__(self, secret_key: str | None = None, timeout: float = 20.0) -> None:
        self.secret_key = secret_key or settings.stripe_secret_key or ""
        self.timeout = timeout

    def _request(self, method: str, path: str, data: dict[str, Any]) -> dict[str, Any]:
        if not self.secret_key:
            raise StripeError("Stripe is not configured (STRIPE_SECRET_KEY is empty)")

        encoded = urlencode(_flatten(data), doseq=True)
        try:
            response = requests.request(
                method,
                f"{BASE_URL}{path}",
                # Stripe's API is form-encoded, not JSON.
                data=encoded if method == "POST" else None,
                params=encoded if method == "GET" else None,
                auth=(self.secret_key, ""),
                headers={
                    "Stripe-Version": API_VERSION,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise StripeError(f"Could not reach Stripe: {exc}") from exc

        if response.status_code >= 400:
            raise StripeError(
                f"Stripe returned {response.status_code}: {_error_message(response)}",
                # 402 means the *card* was declined, which the customer needs
                # to see verbatim rather than as "billing is broken".
                status_code=402 if response.status_code == 402 else 502,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise StripeError("Stripe returned a non-JSON response") from exc

    def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, data)

    def _get(self, path: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", path, data or {})

    # -- API surface ----------------------------------------------------
    def create_customer(
        self, email: str, name: str = "", metadata: dict[str, Any] | None = None
    ) -> str:
        payload: dict[str, Any] = {"email": email}
        if name:
            payload["name"] = name
        for key, value in (metadata or {}).items():
            payload[f"metadata[{key}]"] = str(value)
        return str(self._post("/customers", payload)["id"])

    def create_checkout_session(
        self,
        *,
        customer_id: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        client_reference_id: str = "",
        trial_days: int = 0,
    ) -> str:
        payload: dict[str, Any] = {
            "mode": "subscription",
            "customer": customer_id,
            "line_items[0][price]": price_id,
            "line_items[0][quantity]": 1,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "allow_promotion_codes": "true",
        }
        if client_reference_id:
            # Echoed back on the webhook, which is how a Stripe customer is
            # mapped back to a tenant without trusting anything from a browser.
            payload["client_reference_id"] = client_reference_id
            payload["metadata[organization_id]"] = client_reference_id
        if trial_days > 0:
            payload["subscription_data[trial_period_days]"] = trial_days
        return str(self._post("/checkout/sessions", payload)["url"])

    def create_portal_session(self, *, customer_id: str, return_url: str) -> str:
        result = self._post(
            "/billing_portal/sessions", {"customer": customer_id, "return_url": return_url}
        )
        return str(result["url"])

    def get_subscription(self, subscription_id: str) -> dict[str, Any]:
        return self._get(f"/subscriptions/{subscription_id}")


# ---------------------------------------------------------------------------
# Webhook signatures
# ---------------------------------------------------------------------------
def verify_signature(
    payload: bytes, signature_header: str, secret: str, tolerance: int | None = None
) -> bool:
    """Verify a `Stripe-Signature` header over the **raw** request body.

    The raw body matters: re-serialising the JSON changes the bytes, and the
    HMAC then fails. Three things are checked:

    1. The signature itself (constant-time compare).
    2. The timestamp, so a captured request cannot be replayed later.
    3. That a signature for a *different* timestamp does not also validate.
    """
    if not secret or not signature_header:
        return False

    window = tolerance if tolerance is not None else WEBHOOK_TOLERANCE_SECONDS
    try:
        timestamp_parts: dict[str, list[str]] = {}
        for element in signature_header.split(","):
            key, _, value = element.strip().partition("=")
            timestamp_parts.setdefault(key, []).append(value)
        signatures = timestamp_parts.get("v1", [])
        timestamps = timestamp_parts.get("t", [])
        if not signatures or not timestamps:
            return False
        sent_at = int(timestamps[0])
    except (ValueError, IndexError):
        return False

    if window and abs(int(time.time()) - sent_at) > window:
        logger.warning("Rejected a Stripe webhook with a stale timestamp")
        return False

    expected = hmac.new(
        secret.encode(), f"{sent_at}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    # Check every v1: Stripe sends one per signing secret during rotation.
    return any(hmac.compare_digest(expected, candidate) for candidate in signatures)


def parse_event(payload: bytes) -> dict[str, Any]:
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise StripeError("Webhook body was not valid JSON") from exc

