"""Shared test fixtures: isolated SQLite DB, deterministic LLM, test client."""

from __future__ import annotations

import os

# One database file per process. Two pytest runs sharing a file collide on
# CREATE TABLE and produce failures that look like product bugs but are pure
# harness interference.
_DATABASE_URL = f"sqlite:///./test_sparton_{os.getpid()}.db"

os.environ["DATABASE_URL"] = _DATABASE_URL
os.environ["LLM_PROVIDER"] = "test"
os.environ["AGENT_RUN_INLINE"] = "true"
os.environ["EMBEDDED_WORKER"] = "false"
# Settings also read a developer's local .env. Real credentials there must never
# reach the suite: tests that assert "no key -> not available" would fail, and
# anything that slipped past the deterministic provider could spend real money.
# Environment variables win over .env, so blanking them here is enough.
for _secret in ("OPENAI_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "SMTP_PASSWORD", "API_KEY"):
    os.environ[_secret] = ""
# Force the offline hash embedding backend. Without this the RAG tests fall
# through to sentence-transformers and block on a HuggingFace download, which
# made `pytest` hang forever rather than fail (see docs/DECISIONS.md D-025).
os.environ.setdefault("EMBEDDING_BACKEND", "hash")
# Keep the RAG index out of the repository's data/ directory.
os.environ.setdefault("RAG_DIR", "./.pytest-data/rag")

import atexit  # noqa: E402
import pathlib  # noqa: E402

# SQLite files are not in .gitignore patterns that cover every PID, and a
# crashed run can leave one behind. Clean up on exit, and at import.
_DB_FILE = pathlib.Path(_DATABASE_URL.split("sqlite:///")[-1])


def _remove_db() -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = pathlib.Path(str(_DB_FILE) + suffix)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


atexit.register(_remove_db)
_remove_db()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.core.database.base import Base, SessionLocal, engine  # noqa: E402
from app.llm import DeterministicProvider, set_llm_override  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture()
def db_session():
    """A clean schema for one test.

    ``checkfirst`` matters: a previous test may have left a table behind if it
    was interrupted, and a bare CREATE TABLE then fails on a database that is
    logically clean. ``drop_all`` is scoped to the metadata, never the file.
    """
    Base.metadata.drop_all(bind=engine, checkfirst=True)
    Base.metadata.create_all(bind=engine, checkfirst=True)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine, checkfirst=True)


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


TEST_ADMIN_EMAIL = "admin@example.com"


@pytest.fixture()
def auth_headers(client):
    """Register a fresh org admin and return bearer headers."""
    response = client.post(
        "/auth/register",
        json={
            "email": TEST_ADMIN_EMAIL,
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
def all_domains_client(db_session, llm):
    """A client with every domain enabled.

    The documents / generation / datasets / training domains are feature-flagged
    off by default (docs/DECISIONS.md D-020), so their routes do not exist in a
    default deployment. Tests for them use this, which doubles as a direct test
    of the flag mechanism: the same app factory returns a different surface
    depending on configuration.
    """
    from fastapi.testclient import TestClient

    from app.core.config import settings
    from app.main import create_app

    # The experimental routers need requirements-experimental.txt, which the
    # product image and CI do not install (test_product_dependencies.py checks
    # that they refuse to start without it). Without the extras these tests skip.
    try:
        import app.api.create  # noqa: F401
        import app.api.knowledge  # noqa: F401
    except ImportError as exc:
        pytest.skip(f"needs requirements-experimental.txt ({exc})")

    saved = settings.enabled_domains
    settings.enabled_domains = ",".join([
        "intelligence", "commerce", "agent", "research",
        "documents", "generation", "datasets", "training",
    ])
    try:
        with TestClient(create_app()) as client:
            yield client
    finally:
        settings.enabled_domains = saved


@pytest.fixture()
def all_domain_headers(all_domains_client):
    """Auth headers for a fresh org, against the all-domains client."""
    response = all_domains_client.post(
        "/auth/register",
        json={"email": TEST_ADMIN_EMAIL, "password": "correct-horse-battery",
              "organization_name": "Test Org"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


@pytest.fixture()
def seed_products(db_session, auth_headers):
    """Catalog rows owned by the authenticated caller's organization.

    Seeding with organization_id=None would be correctly filtered out by
    tenant scoping, so every request would see an empty catalog.
    """
    from app.core.database.domain_models import Inventory, Product
    from app.core.database.identity import User

    org_id = db_session.execute(
        select(User).where(User.email == TEST_ADMIN_EMAIL)
    ).scalars().first().organization_id

    products = []
    for i in range(1, 6):
        p = Product(
            organization_id=org_id,
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
