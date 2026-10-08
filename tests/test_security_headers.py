"""Every response carries the security headers; HSTS only in production."""

import pytest

from app.core.config import settings

HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
}


@pytest.mark.parametrize("path", ["/", "/app/", "/billing/plans", "/auth/me", "/definitely-not-a-route"])
def test_security_headers_on_pages_api_and_errors(client, path):
    response = client.get(path)
    for name, value in HEADERS.items():
        assert response.headers.get(name) == value, (path, name)
    assert "strict-transport-security" not in response.headers  # development serves plain http


def test_hsts_only_in_production(client, monkeypatch):
    monkeypatch.setattr(settings, "sparton_env", "production")
    assert client.get("/billing/plans").headers.get("strict-transport-security") == "max-age=31536000"
