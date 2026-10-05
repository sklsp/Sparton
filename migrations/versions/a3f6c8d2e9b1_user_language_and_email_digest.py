"""persist the account language and claim weekly report digests

Two small columns for the email digest (D-032):

* ``users.language`` -- the UI language, persisted so the weekly report email
  can be written in it. The choice used to live only in localStorage, which a
  server-side job cannot read. Server default "nl": Dutch is the product's
  home market and what the UI picks for a Dutch browser anyway.

* ``users.weekly_digest_enabled`` -- the settings toggle. Server default true:
  plans that include the digest start with it on, so an upgrade to Pro does
  not require a click before the first email arrives.

* ``reports.email_digest_sent`` -- the idempotency claim. The weekly job can
  be retried after a crash between "report committed" and "mail sent"; this
  flag is set by an atomic check-and-set UPDATE, so exactly one attempt wins
  and a retry cannot email the same period twice.

All three are NOT NULL with server defaults, so existing rows get sane values
without a backfill pass; no data is invented for anything we do not know.

Reversible: dropping the columns loses nothing that was not already lost by
the emails themselves.

Revision ID: a3f6c8d2e9b1
Revises: f1b7c4e2a9d3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a3f6c8d2e9b1"
down_revision: str | None = "f1b7c4e2a9d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(
            sa.Column("language", sa.String(length=5), nullable=False, server_default="nl")
        )
        batch.add_column(
            sa.Column(
                "weekly_digest_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            )
        )
    with op.batch_alter_table("reports") as batch:
        batch.add_column(
            sa.Column(
                "email_digest_sent", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("weekly_digest_enabled")
        batch.drop_column("language")
    with op.batch_alter_table("reports") as batch:
        batch.drop_column("email_digest_sent")