"""store prices as Numeric(12, 4) so money is Decimal end to end

The product's claim is that it reports a competitor's price exactly. A Float
column quietly contradicts that: 0.45 has no exact binary representation, so it
comes back as 0.45000000000000001, and that number reaches a customer and a
price-change comparison -- the two places where being wrong is expensive.

`Numeric(12, 4)` is fixed-point in the database, and `asdecimal=True` makes
SQLAlchemy hand Python a `Decimal` instead of a float. Four places is two more
than any real price needs and matches the scale the shop feeds publish, so
nothing is lost in the round trip.

NULLABLE-SAFE. The existing columns are nullable and so are the new ones, and
the conversion is done with `CAST(... AS NUMERIC)` where the column already
holds a value. No default is invented for a price we do not know.

Reversible, and the round trip is asserted by a test rather than assumed: a
`Float` column converts back to `FLOAT` cleanly, though of course the exactness
it regains is float exactness.

Revision ID: f1b7c4e2a9d3
Revises: c4d91f2ab7e3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "f1b7c4e2a9d3"
down_revision: str | None = "c4d91f2ab7e3"
branch_labels = None
depends_on = None

#: Every money column, and the table it lives in. Kept in one place because the
#: upgrade and the downgrade must agree, and a mismatch here is a migration that
#: fails halfway.
_MONEY_COLUMNS = (
    ("competitor_products", "price"),
    ("competitor_products", "compare_at_price"),
    ("change_events", "previous_price"),
    ("change_events", "new_price"),
    ("change_events", "delta"),
)


def upgrade() -> None:
    for table, column in _MONEY_COLUMNS:
        with op.batch_alter_table(table) as batch:
            # CAST rather than a bare ALTER: on PostgreSQL the values already in
            # the column are float, and changing the type without converting
            # them would reinterpret the bytes rather than the value.
            batch.alter_column(
                column,
                existing_type=sa.Float(),
                type_=sa.Numeric(12, 4, asdecimal=True),
                existing_nullable=True,
                postgresql_using=f"{column}::numeric(12, 4)",
            )


def downgrade() -> None:
    for table, column in _MONEY_COLUMNS:
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                column,
                existing_type=sa.Numeric(12, 4, asdecimal=True),
                type_=sa.Float(),
                existing_nullable=True,
                postgresql_using=f"{column}::double precision",
            )
