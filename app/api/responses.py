"""Shared error-response metadata for FastAPI route declarations.

FastAPI only lists a route's non-2xx outcomes in /docs when they are declared
in ``responses=``; these constants keep every router describing the same errors
the same way. The body of an ``HTTPException(status, detail=...)`` is always
``{"detail": "..."}``, so one model covers them all.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

#: FastAPI's `responses=` type: status code (int, or a str like "4XX") to an OpenAPI response.
type Responses = dict[int | str, dict[str, Any]]


class ErrorResponse(BaseModel):
    """The body FastAPI returns for every HTTPException: ``{"detail": "..."}``."""

    detail: str


#: The request was rejected; the detail field says why.
BAD_REQUEST: Responses = {
    400: {"description": "The request was rejected; see the detail field for the reason.", "model": ErrorResponse}
}

#: Not signed in, or the session/API key is missing or invalid.
UNAUTHORIZED: Responses = {
    401: {"description": "Not signed in, or the session or API key is missing or invalid.", "model": ErrorResponse}
}

#: Signed in, but not allowed to do this (for example an admin-only action).
FORBIDDEN: Responses = {
    403: {"description": "Signed in, but not allowed to perform this action.", "model": ErrorResponse}
}

#: The organization's plan does not include what the call needs.
PAYMENT_REQUIRED: Responses = {
    402: {"description": "The organization's current plan does not allow this.", "model": ErrorResponse}
}

#: No such resource for this caller (or it belongs to another organization).
NOT_FOUND: Responses = {
    404: {"description": "No such resource exists for this caller.", "model": ErrorResponse}
}

#: The request conflicts with the current state of the resource.
CONFLICT: Responses = {
    409: {"description": "The request conflicts with the current state of the resource.", "model": ErrorResponse}
}

#: Too many requests in a short window; slow down and retry later.
TOO_MANY_REQUESTS: Responses = {
    429: {"description": "Too many requests; the rate limit was exceeded.", "model": ErrorResponse}
}

#: The request body exceeds the size cap for this endpoint.
TOO_LARGE: Responses = {
    413: {"description": "The request body is too large.", "model": ErrorResponse}
}

_BY_CODE: Responses = {
    **BAD_REQUEST,
    **UNAUTHORIZED,
    **FORBIDDEN,
    **PAYMENT_REQUIRED,
    **NOT_FOUND,
    **CONFLICT,
    **TOO_MANY_REQUESTS,
    **TOO_LARGE,
}


def errors(*codes: int) -> Responses:
    """Merge the shared error responses for ``codes`` into one ``responses=`` dict.

    A route writes ``responses=errors(401, 404)``; a code with no shared entry
    raises KeyError so a typo cannot silently drop an error from the docs.
    """
    return {code: _BY_CODE[code] for code in codes}


__all__ = [
    "BAD_REQUEST",
    "CONFLICT",
    "ErrorResponse",
    "FORBIDDEN",
    "NOT_FOUND",
    "PAYMENT_REQUIRED",
    "TOO_LARGE",
    "TOO_MANY_REQUESTS",
    "UNAUTHORIZED",
    "errors",
]
