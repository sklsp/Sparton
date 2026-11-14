"""The shared error-response metadata routers declare with responses=.

Covers app/api/responses.py: the helper returns exactly the requested codes,
an unknown code is a loud KeyError instead of a silently missing doc entry,
and each constant documents its own status. The 413 webhook behaviour itself
is covered by tests/test_billing.py (test_oversized_body_is_rejected).

The second half keeps /docs from regressing as routes change: it reads the
live app (published spec, route dependency graphs, handler sources) instead
of a hand-maintained table, so a new or altered route cannot ship with
missing metadata without failing here. The per-route code table in
tests/test_openapi_errors.py pins runtime-computed codes these guards skip.
"""

from __future__ import annotations

import inspect
import re

import pytest
from fastapi.routing import APIRoute

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


# --- OpenAPI regression guards -------------------------------------------------

@pytest.fixture(scope="module")
def default_app():
    """A fresh app with the default domains: exactly what /docs serves."""
    from app.main import create_app

    return create_app()


def _spec_operations(spec):
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method == "head":
                continue
            yield method.upper(), path, op


def _routes_by_operation(app):
    """Map every (METHOD, path) pair to its APIRoute.

    Built from app.routes rather than the spec so dependency and source
    information stays reachable; operations without a match are skipped by
    callers instead of failing, which keeps these guards focused on metadata.
    """
    routes = {}
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                routes[(method.upper(), route.path)] = route
    return routes


def _documented_codes(operation) -> set[int]:
    return {int(code) for code in operation.get("responses", {}) if str(code).isdigit()}


def test_every_operation_has_a_description(default_app):
    """The description is what /docs renders; an empty one means a missing docstring."""
    spec = default_app.openapi()
    missing = [
        f"{method} {path}"
        for method, path, op in _spec_operations(spec)
        if not (op.get("description") or "").strip()
    ]
    assert not missing, f"operations without a description: {missing}"


def test_protected_operations_document_401(default_app):
    """Every operation whose dependency graph can raise 401 must list it.

    Dependencies are read from the route's dependant graph (app.routes), so a
    handler that gains an auth dependency is caught even if its source never
    mentions current_user by name. The two resolvers in app.core.auth.api are
    recognised by identity because they delegate the 401 to _authenticate; any
    other dependency counts if it raises 401 in its own body.
    """
    from app.core.auth.api import current_user, unverified_user

    auth_ids = {id(current_user), id(unverified_user)}

    def can_raise_401(callable) -> bool:
        if id(callable) in auth_ids:
            return True
        try:
            source = inspect.getsource(callable)
        except (OSError, TypeError):
            return False
        return "HTTP_401" in source or re.search(r"HTTPException\(\s*401\b", source) is not None

    def graph_has_auth(dependant) -> bool:
        stack = list(dependant.dependencies)
        while stack:
            dep = stack.pop()
            if can_raise_401(dep.call):
                return True
            stack.extend(dep.dependencies)
        return False

    spec = default_app.openapi()
    routes = _routes_by_operation(default_app)
    missing = []
    for method, path, op in _spec_operations(spec):
        route = routes.get((method, path))
        if route is None or not graph_has_auth(route.dependant):
            continue
        if 401 not in _documented_codes(op):
            missing.append(f"{method} {path}")
    assert not missing, f"protected operations that do not document 401: {missing}"


# The three literal forms a handler can use to raise a fixed status code.
_LITERAL_STATUS = re.compile(
    r"HTTP_(\d{3})_[A-Z_]+"          # HTTPException(status.HTTP_404_NOT_FOUND, ...)
    r"|status_code\s*=\s*(\d{3})\b"  # HTTPException(status_code=409, ...)
    r"|HTTPException\(\s*(\d{3})\b"  # HTTPException(404, ...)
)


def test_literal_raises_are_documented(default_app):
    """Every status a handler raises literally in its own source appears in responses.

    Only direct literals count: the three forms above. Codes computed at
    runtime (status_code=exc.status_code) are out of scope by design; the
    per-route table in tests/test_openapi_errors.py pins those instead.
    """
    spec = default_app.openapi()
    routes = _routes_by_operation(default_app)

    missing = []
    for method, path, op in _spec_operations(spec):
        route = routes.get((method, path))
        if route is None:
            continue
        try:
            source = inspect.getsource(route.endpoint)
        except (OSError, TypeError):
            continue
        raised = {int(a or b or c) for a, b, c in _LITERAL_STATUS.findall(source)}
        absent = sorted(raised - _documented_codes(op))
        if absent:
            missing.append(f"{method} {path}: {absent}")
    assert not missing, f"literally raised statuses missing from responses: {missing}"
