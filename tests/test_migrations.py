"""The migration must actually build the schema.

Phase 1 found that the only Alembic revision was `upgrade() -> pass`: a fresh
production database would come up with zero tables and the app would fail on
its first query. A migration that is merely non-empty is not much better, so
these tests run it for real against a throwaway SQLite file.

They are slower than the rest of the suite (each spawns an Alembic subprocess)
because there is no honest way to test a migration without running it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{db_path}",
        "SPARTON_ENV": "development",
    }
    return subprocess.run(
        [sys.executable, "-W", "ignore", "-m", "alembic", *args],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.fixture()
def migrated_db(tmp_path):
    """An empty database at `head`, i.e. what production actually starts with."""
    db = tmp_path / "schema.db"
    result = _alembic(db, "upgrade", "head")
    assert result.returncode == 0, result.stderr[-2000:]
    return db


class TestMigrationIsNotEmpty:
    def test_no_shipped_revision_is_a_stub(self):
        """Guard the original defect directly. An empty `upgrade()` passes every
        other test here, because they read the same empty result."""
        versions = list((REPO_ROOT / "migrations" / "versions").glob("*.py"))
        assert versions, "no migrations at all"
        for path in versions:
            source = path.read_text(encoding="utf-8", errors="replace")
            body = source.split("def upgrade", 1)[-1].split("def downgrade", 1)[0]
            # `batch_alter_table(...).add_column(...)` is the same operation as
            # `op.add_column`, so the check has to look for the call on either
            # side of the dot rather than only the bare form.
            mutating = any(
                token in body
                for token in (
                    "op.create_table",
                    "op.execute",
                    "op.add_column",
                    "op.drop_column",
                    "op.create_index",
                    "batch_op.add_column",
                    "batch.add_column",
                    "batch_op.create_table",
                    "batch.create_table",
                    # A type change is a real migration too: the money columns
                    # moved from Float to Numeric without being added or dropped.
                    "op.alter_column",
                    "batch.alter_column",
                )
            )
            assert mutating, f"{path.name} has an empty upgrade()"

    def test_the_latest_revision_can_be_upgraded_and_downgraded(self, tmp_path):
        """A migration that only goes forwards leaves no way back.

        Not every change is reversible, but a column that was added can be
        dropped again, and this one can -- so the round trip is asserted rather
        than assumed.
        """
        db = tmp_path / "roundtrip.db"
        up = _alembic(db, "upgrade", "head")
        assert up.returncode == 0, up.stderr[-2000:]
        down = _alembic(db, "downgrade", "-1")
        assert down.returncode == 0, down.stderr[-2000:]
        back = _alembic(db, "upgrade", "head")
        assert back.returncode == 0, back.stderr[-2000:]


class TestSchemaIsBuilt:
    def test_upgrade_head_creates_every_core_table(self, migrated_db):
        from sqlalchemy import create_engine, inspect

        tables = set(inspect(create_engine(f"sqlite:///{migrated_db}")).get_table_names())
        required = {
            # identity
            "organizations", "users", "sessions", "auth_tokens",
            # the product loop
            "shops", "competitors", "competitor_products", "products",
            "product_snapshots", "inventory", "change_events", "reports", "jobs",
            # billing
            "subscriptions", "invoices", "stripe_events",
            # cost accounting
            "llm_usage",
        }
        assert required <= tables, f"missing: {sorted(required - tables)}"

    def test_the_schema_matches_the_models(self, tmp_path):
        """A hand-edited migration drifts silently. Autogenerating again must
        produce nothing, or the next deploy will fail against a real database.
        """
        db = tmp_path / "drift.db"
        assert _alembic(db, "upgrade", "head").returncode == 0

        result = _alembic(db, "revision", "--autogenerate", "-m", "drift_probe")
        assert result.returncode == 0, result.stderr[-2000:]

        versions = REPO_ROOT / "migrations" / "versions"
        newest = max(versions.glob("*.py"), key=lambda p: p.stat().st_mtime)
        try:
            body = newest.read_text(encoding="utf-8", errors="replace")
            assert "op.create_table" not in body, "models contain tables the migration does not create"
            assert "op.drop_table" not in body, "migration drops a table the models still declare"
        finally:
            newest.unlink(missing_ok=True)

    def test_downgrade_then_upgrade_round_trips(self, tmp_path):
        """A migration that cannot be reversed cannot be debugged in production.
        Note SQLite's batch mode, which `migrations/env.py` enables."""
        db = tmp_path / "roundtrip.db"
        assert _alembic(db, "upgrade", "head").returncode == 0
        down = _alembic(db, "downgrade", "base")
        assert down.returncode == 0, down.stderr[-2000:]
        up = _alembic(db, "upgrade", "head")
        assert up.returncode == 0, up.stderr[-2000:]
