"""Every response carries the security headers; HSTS only in production."""

import pytest

from pathlib import Path
import re

from app.core.config import settings
from app.main import CSP

HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": CSP,
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


def test_no_page_has_an_inline_script():
    """script-src 'self' blocks inline scripts, so a page that adds one would silently break."""
    web = Path(__file__).resolve().parents[1] / "app" / "web"
    offenders = []
    for page in web.rglob("*.html"):
        for attrs, body in re.findall(r"<script\b([^>]*)>(.*?)</script>", page.read_text(encoding="utf-8"), re.S):
            if "src=" not in attrs and body.strip():
                offenders.append(str(page.relative_to(web)))
        if re.search(r"\son[a-z]+\s*=\s*[\"']", page.read_text(encoding="utf-8")):
            offenders.append(f"{page.relative_to(web)} (inline event handler)")
    assert offenders == []
