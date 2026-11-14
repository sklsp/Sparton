"""The shared error-response metadata routers declare with responses=.

Covers app/api/responses.py: the helper returns exactly the requested codes,
an unknown code is a loud KeyError instead of a silently missing doc entry,
and each constant documents its own status. The 413 webhook behaviour itself
is covered by tests/test_billing.py (test_oversized_body_is_rejected).
"""

from __future__ import annotations

import pytest

from app.api import responses as r
from app.api.responses import ErrorResponse, errors


def test_errors_returns_exactly_the_requested_codes():
    result = errors(401, 404)
    assert set(result) == {401, 404}
    for code in (401, 404):
        entry = result[code]
        assert isinstance(entry["description"], str) and entry["description"]
        assert entry["model"] is ErrorResponse


def test_unknown_code_raises_keyerror():
    with pytest.raises(KeyError):
        errors(999)


def test_each_constant_documents_its_own_status():
    expected = [
        (r.UNAUTHORIZED, 401),
        (r.FORBIDDEN, 403),
        (r.PAYMENT_REQUIRED, 402),
        (r.NOT_FOUND, 404),
        (r.CONFLICT, 409),
        (r.TOO_MANY_REQUESTS, 429),
        (r.TOO_LARGE, 413),
    ]
    for constant, code in expected:
        assert list(constant) == [code]
