"""report language (v1.1): organizations.language and reports.language

The weekly report's prose is written in the account's language. Existing
accounts and reports read "en", which is what they were written in.

Revision ID: b3c8e1f04d27
Revises: a7e2d5c1b9f4
Create Date: 2026-10-05 11:41:56.728814
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = 'b3c8e1f04d27'
down_revision: str | None = 'a7e2d5c1b9f4'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('language', sa.String(length=8), server_default='en', nullable=False))

    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.add_column(sa.Column('language', sa.String(length=8), server_default='en', nullable=False))



def downgrade() -> None:
    with op.batch_alter_table('reports', schema=None) as batch_op:
        batch_op.drop_column('language')

    with op.batch_alter_table('organizations', schema=None) as batch_op:
        batch_op.drop_column('language')

