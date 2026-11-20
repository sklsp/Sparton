"""API tests for the feature-flagged create domain (generation, datasets, training)."""

from __future__ import annotations


def test_create_routes_exist_without_documents(create_domains_client):
    """A create route answers 401 without headers, not 404.

    A disabled domain has no routes at all (docs/DECISIONS.md D-020), so a 401
    proves the router was registered and only the auth dependency fired.
    """
    response = create_domains_client.get("/training/status")
    assert response.status_code == 401


def test_documents_routes_absent_from_create_client(create_domains_client):
    """The documents domain stays off in this client, so its routes do not exist."""
    response = create_domains_client.get("/documents")
    assert response.status_code == 404
