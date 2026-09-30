"""competitor data_source, compare_at_price, variant_count, last_source

Exact prices, and an honest record of where each one came from.

Adds the columns that let a capture say "this is 24.99, read from the shop's
own feed" instead of "this is 24.99, we think". `compare_at_price` matters on
its own: without it a sale is invisible, because the effective price is the
same before and after a discount ends -- only the was-price tells you the
markdown was withdrawn.

All nullable or defaulted, so this is safe to apply to a live table: existing
rows read as data_source="html", variant_count=1, compare_at_price=NULL, which
is exactly what we know about them (they were parsed from pages).

Revision ID: c4d91f2ab7e3
Revises: 881cfa48e50c
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "c4d91f2ab7e3"
down_revision: str | None = "881cfa48e50c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("competitor_products") as batch:
        batch.add_column(sa.Column("compare_at_price", sa.Float(), nullable=True))
        batch.add_column(
            sa.Column(
                "data_source", sa.String(length=16), nullable=False, server_default="html"
            )
        )
        batch.add_column(
            sa.Column(
                "variant_count", sa.Integer(), nullable=False, server_default="1"
            )
        )
        batch.create_index(
            "ix_competitor_products_compare_at_price", ["compare_at_price"]
        )
        batch.create_index("ix_competitor_products_data_source", ["data_source"])

    with op.batch_alter_table("competitors") as batch:
        batch.add_column(
            sa.Column("last_source", sa.String(length=16), nullable=False, server_default="")
        )
        batch.create_index("ix_competitors_last_source", ["last_source"])


def downgrade() -> None:
    with op.batch_alter_table("competitors") as batch:
        batch.drop_index("ix_competitors_last_source")
        batch.drop_column("last_source")

    with op.batch_alter_table("competitor_products") as batch:
        batch.drop_index("ix_competitor_products_data_source")
        batch.drop_index("ix_competitor_products_compare_at_price")
        batch.drop_column("variant_count")
        batch.drop_column("data_source")
        batch.drop_column("compare_at_price")
