"""Shared test fixtures: isolated SQLite DB, deterministic LLM, test client."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_sparton.db")
os.environ.setdefault("LLM_PROVIDER", "test")
os.environ.setdefault("AGENT_RUN_INLINE", "true")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.database.base import Base, SessionLocal, engine  # noqa: E402
from app.llm import DeterministicProvider, set_llm_override  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def db_session():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def llm():
    provider = DeterministicProvider()
    set_llm_override(provider)
    yield provider
    set_llm_override(None)


@pytest.fixture()
def client(db_session, llm):
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_headers(client):
    """Register a fresh org admin and return bearer headers."""
    response = client.post(
        "/auth/register",
        json={
            "email": "admin@example.com",
            "password": "correct-horse-battery",
            "organization_name": "Test Org",
        },
    )
    assert response.status_code == 201, response.text
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    from app.core.security.rate_limit import reset_limits

    reset_limits()
    yield
    reset_limits()


@pytest.fixture()
def seed_products(db_session):
    from app.core.database.domain_models import Inventory, Product

    products = []
    for i in range(1, 6):
        p = Product(
            organization_id=None,
            sku=f"SKU-{i}",
            title=f"Product {i}",
            description=f"Description for product {i}",
            price=10.0 * i,
            category="test",
        )
        db_session.add(p)
        db_session.flush()
        db_session.add(Inventory(product_id=p.id, quantity=i * 5))
        products.append(p)
    db_session.commit()
    return products


def reset_db():
    with engine.begin() as conn:
        conn.execute(text("SELECT 1"))
