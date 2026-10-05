"""persist the account language and claim weekly report digests

Two small columns for the email digest (D-032):

* ``users.weekly_digest_enabled`` -- the settings toggle. Server default true:
  plans that include the digest start with it on, so an upgrade to Pro does
  not require a click before the first email arrives.

* ``reports.email_digest_sent`` -- the idempotency claim. The weekly job can
  be retried after a crash between "report committed" and "mail sent"; this
  flag is set by an atomic check-and-set UPDATE, so exactly one attempt wins
  and a retry cannot email the same period twice.

All are NOT NULL with server defaults, so existing rows get sane values
without a backfill pass; no data is invented for anything we do not know.

Reversible: dropping the columns loses nothing that was not already lost by
the emails themselves.

Revision ID: a3f6c8d2e9b1
Revises: b3c8e1f04d27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a3f6c8d2e9b1"
down_revision: str | None = "b3c8e1f04d27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
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
    with op.batch_alter_table("reports") as batch:
        batch.drop_column("email_digest_sent")