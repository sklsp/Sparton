"""Unit tests for app/billing/stripe.py: the raw-requests Stripe client.

Every HTTP call goes through a monkeypatched ``requests.request``, so nothing
in this file can reach the network (RULES.md). The webhook helpers are covered
here as well because the per-file coverage gate measures stripe.py on its own,
and test_billing.py's webhook tests do not count toward it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qsl

import pytest
import requests
from requests.models import Response

from app.billing.stripe import (
    API_VERSION,
    BASE_URL,
    WEBHOOK_TOLERANCE_SECONDS,
    StripeClient,
    StripeError,
    _error_message,
    _flatten,
    is_configured,
    parse_event,
    verify_signature,
)
from app.core.config import settings


def make_response(status_code: int, body: str | None = None, json_body: Any = None) -> Response:
    """A real ``requests.Response`` with a canned body; no network involved."""
    response = Response()
    if json_body is not None:
        response._content = json.dumps(json_body).encode("utf-8")
    else:
        response._content = (body or "").encode("utf-8")
    response.status_code = status_code
    return response


class FakeStripeHttp:
    """Stands in for ``requests.request`` and records every call it gets."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        response: Response | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.response = response
        self.exc = exc

        def fake_request(method: str, url: str, **kwargs: Any) -> Response:
            self.calls.append((method, url, kwargs))
            if self.exc is not None:
                raise self.exc
            assert self.response is not None
            return self.response

        monkeypatch.setattr(requests, "request", fake_request)

    @property
    def last(self) -> tuple[str, str, dict[str, Any]]:
        method, url, kwargs = self.calls[-1]
        return method, url, kwargs


# ---------------------------------------------------------------------------
# Configuration and helpers
# ---------------------------------------------------------------------------
class TestIsConfigured:
    def test_true_when_a_secret_key_is_set(self, monkeypatch):
        monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_123")
        assert is_configured() is True

    def test_false_when_the_key_is_empty(self, monkeypatch):
        monkeypatch.setattr(settings, "stripe_secret_key", "")
        assert is_configured() is False


class TestFlatten:
    def test_a_flat_dict_passes_through_untouched(self):
        data = {"mode": "subscription", "customer": "cus_1"}
        assert _flatten(data) == data

    def test_a_nested_dict_becomes_bracket_keys(self):
        assert _flatten({"a": {"b": 1}}) == {"a[b]": 1}

    def test_nesting_is_recursive_and_keeps_sibling_keys(self):
        data = {"subscription_data": {"trial_period_days": 7, "metadata": {"org": "42"}}}
        assert _flatten(data) == {
            "subscription_data[trial_period_days]": 7,
            "subscription_data[metadata][org]": "42",
        }

    def test_a_key_that_already_has_brackets_is_a_leaf(self):
        """Pre-built keys must not be expanded into a stringified list."""
        data = {"line_items[0]": {"price": "p_1"}}
        assert _flatten(data) == data

    def test_a_list_value_passes_through_for_doseq_encoding(self):
        items = [{"id": "a"}, {"id": "b"}]
        assert _flatten({"items": items})["items"] is items

    def test_an_explicit_prefix_wraps_every_key(self):
        assert _flatten({"b": 1}, prefix="a") == {"a[b]": 1}


class TestErrorMessage:
    def test_a_json_error_body_yields_the_stripe_message(self):
        response = make_response(400, json_body={"error": {"message": "Your card was declined."}})
        assert _error_message(response) == "Your card was declined."

    def test_an_error_object_without_a_message_is_stringified(self):
        payload = {"error": {"type": "invalid_request_error"}}
        response = make_response(400, json_body=payload)
        assert _error_message(response) == str(payload["error"])[:300]

    def test_a_json_body_without_an_error_key_is_stringified_whole(self):
        payload = {"detail": "boom"}
        response = make_response(500, json_body=payload)
        assert _error_message(response) == str(payload)[:300]

    def test_a_non_dict_json_body_is_stringified_whole(self):
        response = make_response(500, body="[1, 2]")
        assert _error_message(response) == "[1, 2]"

    def test_a_non_json_body_falls_back_to_the_raw_text(self):
        response = make_response(502, body="Bad Gateway")
        assert _error_message(response) == "Bad Gateway"

    def test_long_bodies_are_truncated(self):
        response = make_response(500, body="x" * 500)
        assert len(_error_message(response)) == 200


# ---------------------------------------------------------------------------
# StripeClient._request
# ---------------------------------------------------------------------------
class TestRequest:
    def test_a_missing_secret_key_raises_before_any_http(self, monkeypatch):
        http = FakeStripeHttp(monkeypatch, response=make_response(200, json_body={}))
        client = StripeClient(secret_key="")
        with pytest.raises(StripeError, match="not configured"):
            client._post("/customers", {})
        assert http.calls == []

    def test_post_sends_the_form_body_and_returns_json(self, monkeypatch):
        http = FakeStripeHttp(monkeypatch, response=make_response(200, json_body={"id": "cus_1"}))
        client = StripeClient(secret_key="sk_test")
        result = client._post("/customers", {"email": "a@b.test"})
        assert result == {"id": "cus_1"}
        method, url, kwargs = http.last
        assert (method, url) == ("POST", BASE_URL + "/customers")
        # `data` is the already-urlencoded body, so parse it back rather than
        # pretending it is still a dict.
        assert dict(parse_qsl(kwargs["data"])) == {"email": "a@b.test"}
        assert kwargs["params"] is None
        assert kwargs["auth"] == ("sk_test", "")
        assert kwargs["headers"]["Stripe-Version"] == API_VERSION
        assert kwargs["timeout"] == 20.0

    def test_get_puts_the_data_in_query_params_not_a_body(self, monkeypatch):
        http = FakeStripeHttp(monkeypatch, response=make_response(200, json_body={"id": "sub_1"}))
        client = StripeClient(secret_key="sk_test")
        result = client._get("/subscriptions/sub_1", {"expand[0]": "items"})
        assert result == {"id": "sub_1"}
        method, url, kwargs = http.last
        assert (method, url) == ("GET", BASE_URL + "/subscriptions/sub_1")
        assert dict(parse_qsl(kwargs["params"])) == {"expand[0]": "items"}
        assert kwargs["data"] is None

    def test_a_client_error_becomes_a_stripe_error_with_status_502(self, monkeypatch):
        FakeStripeHttp(
            monkeypatch, response=make_response(400, json_body={"error": {"message": "No such customer"}})
        )
        with pytest.raises(StripeError) as exc:
            StripeClient(secret_key="sk_test")._post("/customers", {})
        assert exc.value.status_code == 502
        assert "Stripe returned 400" in str(exc.value)
        assert "No such customer" in str(exc.value)

    def test_a_server_error_becomes_a_stripe_error_with_status_502(self, monkeypatch):
        FakeStripeHttp(monkeypatch, response=make_response(503, body="Service Unavailable"))
        with pytest.raises(StripeError) as exc:
            StripeClient(secret_key="sk_test")._post("/customers", {})
        assert exc.value.status_code == 502
        assert "Stripe returned 503" in str(exc.value)
        assert "Service Unavailable" in str(exc.value)

    def test_a_declined_card_keeps_status_402(self, monkeypatch):
        """402 means the card was declined; the customer must see it verbatim."""
        FakeStripeHttp(
            monkeypatch, response=make_response(402, json_body={"error": {"message": "Your card was declined."}})
        )
        with pytest.raises(StripeError) as exc:
            StripeClient(secret_key="sk_test")._post("/checkout/sessions", {})
        assert exc.value.status_code == 402
        assert "declined" in str(exc.value)

    def test_a_network_error_becomes_a_stripe_error(self, monkeypatch):
        FakeStripeHttp(monkeypatch, exc=requests.ConnectionError("connection refused"))
        with pytest.raises(StripeError, match="Could not reach Stripe"):
            StripeClient(secret_key="sk_test")._post("/customers", {})

    def test_a_non_json_success_body_is_an_error(self, monkeypatch):
        FakeStripeHttp(monkeypatch, response=make_response(200, body="<html>nope</html>"))
        with pytest.raises(StripeError, match="non-JSON"):
            StripeClient(secret_key="sk_test")._post("/customers", {})

    def test_the_secret_key_falls_back_to_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "stripe_secret_key", "sk_from_env")
        client = StripeClient()
        assert client.secret_key == "sk_from_env"
        assert client.timeout == 20.0

    def test_an_empty_everywhere_gives_an_empty_key(self, monkeypatch):
        monkeypatch.setattr(settings, "stripe_secret_key", "")
        client = StripeClient(timeout=5.0)
        assert client.secret_key == ""
        assert client.timeout == 5.0


# ---------------------------------------------------------------------------
# The public API surface: path and form data of every call
# ---------------------------------------------------------------------------
class TestCreateCustomer:
    def test_email_only(self, monkeypatch):
        http = FakeStripeHttp(monkeypatch, response=make_response(200, json_body={"id": "cus_9"}))
        result = StripeClient(secret_key="sk_test").create_customer("a@b.test")
        assert result == "cus_9"
        method, url, kwargs = http.last
        assert (method, url) == ("POST", BASE_URL + "/customers")
        assert dict(parse_qsl(kwargs["data"])) == {"email": "a@b.test"}

    def test_name_and_metadata_are_added(self, monkeypatch):
        http = FakeStripeHttp(monkeypatch, response=make_response(200, json_body={"id": "cus_9"}))
        StripeClient(secret_key="sk_test").create_customer("a@b.test", name="Ada", metadata={"org": 42})
        method, url, kwargs = http.last
        assert (method, url) == ("POST", BASE_URL + "/customers")
        # Metadata values are stringified on the way out.
        assert dict(parse_qsl(kwargs["data"])) == {"email": "a@b.test", "name": "Ada", "metadata[org]": "42"}


class TestCreateCheckoutSession:
    def test_the_base_payload(self, monkeypatch):
        http = FakeStripeHttp(
            monkeypatch, response=make_response(200, json_body={"url": "https://checkout.stripe.com/c/pay/cs_1"})
        )
        url = StripeClient(secret_key="sk_test").create_checkout_session(
            customer_id="cus_1",
            price_id="price_pro",
            success_url="https://x.test/ok",
            cancel_url="https://x.test/no",
        )
        assert url == "https://checkout.stripe.com/c/pay/cs_1"
        method, path, kwargs = http.last
        assert (method, path) == ("POST", BASE_URL + "/checkout/sessions")
        body = dict(parse_qsl(kwargs["data"]))
        assert body == {
            "mode": "subscription",
            "customer": "cus_1",
            "line_items[0][price]": "price_pro",
            "line_items[0][quantity]": "1",
            "success_url": "https://x.test/ok",
            "cancel_url": "https://x.test/no",
            "allow_promotion_codes": "true",
        }

    def test_a_client_reference_id_is_echoed_for_the_webhook(self, monkeypatch):
        http = FakeStripeHttp(monkeypatch, response=make_response(200, json_body={"url": "https://x.test/cs"}))
        StripeClient(secret_key="sk_test").create_checkout_session(
            customer_id="cus_1",
            price_id="price_pro",
            success_url="https://x.test/ok",
            cancel_url="https://x.test/no",
            client_reference_id="42",
        )
        body = dict(parse_qsl(http.last[2]["data"]))
        # The tenant id is echoed back, so the webhook can map it without
        # trusting anything from the browser.
        assert body["client_reference_id"] == "42"
        assert body["metadata[organization_id]"] == "42"
        assert "subscription_data[trial_period_days]" not in body

    def test_trial_days_add_subscription_data(self, monkeypatch):
        http = FakeStripeHttp(monkeypatch, response=make_response(200, json_body={"url": "https://x.test/cs"}))
        StripeClient(secret_key="sk_test").create_checkout_session(
            customer_id="cus_1",
            price_id="price_pro",
            success_url="https://x.test/ok",
            cancel_url="https://x.test/no",
            trial_days=14,
        )
        body = dict(parse_qsl(http.last[2]["data"]))
        assert body["subscription_data[trial_period_days]"] == "14"


class TestCreatePortalSession:
    def test_path_and_form_data(self, monkeypatch):
        http = FakeStripeHttp(
            monkeypatch, response=make_response(200, json_body={"url": "https://billing.stripe.com/p/session/ps_1"})
        )
        url = StripeClient(secret_key="sk_test").create_portal_session(
            customer_id="cus_1", return_url="https://x.test/back"
        )
        assert url == "https://billing.stripe.com/p/session/ps_1"
        method, path, kwargs = http.last
        assert (method, path) == ("POST", BASE_URL + "/billing_portal/sessions")
        assert dict(parse_qsl(kwargs["data"])) == {"customer": "cus_1", "return_url": "https://x.test/back"}


class TestGetSubscription:
    def test_path_and_returned_body(self, monkeypatch):
        http = FakeStripeHttp(monkeypatch, response=make_response(200, json_body={"id": "sub_7", "status": "active"}))
        result = StripeClient(secret_key="sk_test").get_subscription("sub_7")
        assert result == {"id": "sub_7", "status": "active"}
        method, path, kwargs = http.last
        assert (method, path) == ("GET", BASE_URL + "/subscriptions/sub_7")
        # A GET with no data still sends an empty query string.
        assert dict(parse_qsl(kwargs["params"])) == {}


# ---------------------------------------------------------------------------
# Webhook signatures and event parsing
# ---------------------------------------------------------------------------
def stripe_signature(payload: bytes, secret: str, timestamp: int) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


class TestVerifySignature:
    def test_a_valid_signature_is_accepted(self):
        payload = b'{"id": "evt_1"}'
        header = stripe_signature(payload, "whsec_test", int(time.time()))
        assert verify_signature(payload, header, "whsec_test") is True

    def test_the_wrong_secret_is_rejected(self):
        payload = b'{"id": "evt_1"}'
        header = stripe_signature(payload, "whsec_other", int(time.time()))
        assert verify_signature(payload, header, "whsec_test") is False

    def test_a_tampered_payload_is_rejected(self):
        header = stripe_signature(b'{"amount": 999}', "whsec_test", int(time.time()))
        assert verify_signature(b'{"amount": 100}', header, "whsec_test") is False

    def test_a_stale_timestamp_is_rejected(self):
        stale = int(time.time()) - WEBHOOK_TOLERANCE_SECONDS - 10
        header = stripe_signature(b"{}", "whsec_test", stale)
        assert verify_signature(b"{}", header, "whsec_test") is False

    def test_a_custom_tolerance_widens_the_window(self):
        old = int(time.time()) - WEBHOOK_TOLERANCE_SECONDS - 10
        header = stripe_signature(b"{}", "whsec_test", old)
        assert verify_signature(b"{}", header, "whsec_test", tolerance=3600) is True

    def test_an_empty_secret_or_header_is_rejected(self):
        payload = b"{}"
        header = stripe_signature(payload, "whsec_test", int(time.time()))
        assert verify_signature(payload, header, "") is False
        assert verify_signature(payload, "", "whsec_test") is False

    def test_a_header_missing_v1_or_t_is_rejected(self):
        assert verify_signature(b"{}", "t=123,v0=abc", "whsec_test") is False
        assert verify_signature(b"{}", "v1=abc", "whsec_test") is False

    def test_a_garbage_timestamp_is_rejected(self):
        assert verify_signature(b"{}", "t=not-a-number,v1=abc", "whsec_test") is False

    def test_rotation_accepts_any_of_the_v1_signatures(self):
        """Stripe sends one v1 per signing secret during rotation."""
        payload = b'{"id": "evt_2"}'
        now = int(time.time())
        old_digest = hmac.new(b"whsec_old", f"{now}.".encode() + payload, hashlib.sha256).hexdigest()
        good_digest = hmac.new(b"whsec_new", f"{now}.".encode() + payload, hashlib.sha256).hexdigest()
        header = f"t={now},v1={old_digest},v1={good_digest}"
        assert verify_signature(payload, header, "whsec_new") is True


class TestParseEvent:
    def test_a_valid_json_body_is_returned(self):
        payload = b'{"id": "evt_1", "type": "checkout.session.completed"}'
        assert parse_event(payload) == {"id": "evt_1", "type": "checkout.session.completed"}

    def test_an_invalid_body_raises_stripe_error(self):
        with pytest.raises(StripeError, match="not valid JSON"):
            parse_event(b"this is not json")

