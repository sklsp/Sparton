"""own catalog and product matches (v1.1, closes D-031)

`shop_products` is the customer's own catalogue as a time series, read with
shopfeed exactly like a competitor. `product_matches` pairs those products with
competitor products (barcode first, then shopfeed's title rules). `gtin` on
competitor captures is what makes the barcode match possible; existing rows get
"" (no barcode known), which is the truth about them.

Revision ID: a7e2d5c1b9f4
Revises: f1b7c4e2a9d3
Create Date: 2026-10-05 11:19:51.730567
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision: str = 'a7e2d5c1b9f4'
down_revision: str | None = 'f1b7c4e2a9d3'
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table('shop_products',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=True),
    sa.Column('shop_id', sa.Integer(), nullable=False),
    sa.Column('source_url', sa.Text(), nullable=False),
    sa.Column('external_id', sa.String(length=160), nullable=False),
    sa.Column('name', sa.String(length=400), nullable=False),
    sa.Column('brand', sa.String(length=200), nullable=False),
    sa.Column('gtin', sa.String(length=32), nullable=False),
    sa.Column('price', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('currency', sa.String(length=8), nullable=False),
    sa.Column('in_stock', sa.Boolean(), nullable=False),
    sa.Column('data_source', sa.String(length=16), nullable=False),
    sa.Column('captured_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['shop_id'], ['shops.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('shop_products', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_shop_products_captured_at'), ['captured_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_shop_products_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_shop_products_shop_id'), ['shop_id'], unique=False)
        batch_op.create_index('ix_sp_shop_url_time', ['shop_id', 'source_url', 'captured_at'], unique=False)

    op.create_table('product_matches',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('organization_id', sa.Integer(), nullable=True),
    sa.Column('shop_id', sa.Integer(), nullable=False),
    sa.Column('competitor_id', sa.Integer(), nullable=False),
    sa.Column('own_url', sa.Text(), nullable=False),
    sa.Column('own_name', sa.String(length=400), nullable=False),
    sa.Column('competitor_url', sa.Text(), nullable=False),
    sa.Column('competitor_name', sa.String(length=400), nullable=False),
    sa.Column('method', sa.String(length=16), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('matched_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['competitor_id'], ['competitors.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['shop_id'], ['shops.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('product_matches', schema=None) as batch_op:
        batch_op.create_index('ix_pm_competitor_url', ['competitor_id', 'competitor_url'], unique=False)
        batch_op.create_index(batch_op.f('ix_product_matches_competitor_id'), ['competitor_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_product_matches_organization_id'), ['organization_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_product_matches_shop_id'), ['shop_id'], unique=False)

    with op.batch_alter_table('competitor_products', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gtin', sa.String(length=32), nullable=False, server_default=''))
        batch_op.create_index(batch_op.f('ix_competitor_products_gtin'), ['gtin'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('competitor_products', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_competitor_products_gtin'))
        batch_op.drop_column('gtin')

    with op.batch_alter_table('product_matches', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_product_matches_shop_id'))
        batch_op.drop_index(batch_op.f('ix_product_matches_organization_id'))
        batch_op.drop_index(batch_op.f('ix_product_matches_competitor_id'))
        batch_op.drop_index('ix_pm_competitor_url')

    op.drop_table('product_matches')
    with op.batch_alter_table('shop_products', schema=None) as batch_op:
        batch_op.drop_index('ix_sp_shop_url_time')
        batch_op.drop_index(batch_op.f('ix_shop_products_shop_id'))
        batch_op.drop_index(batch_op.f('ix_shop_products_organization_id'))
        batch_op.drop_index(batch_op.f('ix_shop_products_captured_at'))

    op.drop_table('shop_products')
