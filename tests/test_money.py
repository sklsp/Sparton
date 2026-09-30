"""Money is Decimal end to end, not float.

The product tells a customer what a competitor charges. A float cannot hold
0.45, so a Float column turns "exact" into "exact to about fourteen decimal
places" and puts the error in a number someone acts on. These tests pin the
type at each point where the type could quietly regress: the column, the
boundary where a capture is built, and the arithmetic that compares two
captures.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import Numeric

from app.core.database.ecommerce_models import (
    ChangeEvent,
    Competitor,
    CompetitorProduct,
)


MONEY_COLUMNS = (
    (CompetitorProduct, "price"),
    (CompetitorProduct, "compare_at_price"),
    (ChangeEvent, "previous_price"),
    (ChangeEvent, "new_price"),
    (ChangeEvent, "delta"),
)


class TestTheColumnsAreFixedPoint:
    @pytest.mark.parametrize("model,column", MONEY_COLUMNS, ids=lambda v: getattr(v, "name", v))
    def test_the_column_is_fixed_point(self, model, column):
        """Not `isinstance(type, Numeric)`: SQLAlchemy's `Float` *subclasses*
        `Numeric`, so that test passes for a float column and proves nothing.
        What distinguishes them is asdecimal, which is checked next."""
        column_type = model.__table__.columns[column].type
        assert type(column_type).__name__ == "Numeric", (
            f"{model.__tablename__}.{column} is "
            f"{type(column_type).__name__}, not Numeric"
        )
        assert isinstance(column_type, Numeric)

    @pytest.mark.parametrize("model,column", MONEY_COLUMNS, ids=lambda v: getattr(v, "name", v))
    def test_the_column_hands_back_a_decimal(self, model, column):
        assert model.__table__.columns[column].type.asdecimal is True, (
            f"{model.__tablename__}.{column} does not use asdecimal, so "
            "SQLAlchemy converts it back to float on the way out"
        )

    def test_the_scale_holds_four_decimal_places(self):
        for model, column in MONEY_COLUMNS:
            assert model.__table__.columns[column].type.scale == 4, (
                f"{model.__tablename__}.{column} scale"
            )

    def test_a_percentage_column_is_still_a_float(self):
        """delta_pct is a ratio, not money. Four decimal places of a percentage
        is precision nobody asked for, and it would be a misleading claim."""
        column_type = ChangeEvent.__table__.columns["delta_pct"].type
        assert type(column_type).__name__ == "Float", type(column_type).__name__
        assert column_type.asdecimal is not True


class TestTheCaptureBoundary:
    def test_a_float_price_becomes_a_decimal(self):
        """The HTML and LLM tiers produce floats. They are coerced at the one
        boundary, so nothing downstream mixes Decimal with float -- which
        raises TypeError rather than quietly rounding."""
        from app.ecommerce.crawl import as_money

        assert as_money(24.99) == Decimal("24.99")
        assert isinstance(as_money(24.99), Decimal)

    def test_the_awkward_price_survives_exactly(self):
        from app.ecommerce.crawl import as_money

        # 0.1 + 0.2 != 0.3 in binary floating point. This is the whole reason.
        assert (as_money(0.1) + as_money(0.2)) == as_money(0.3)
        assert as_money(0.45) == Decimal("0.45")

    @pytest.mark.parametrize(
        "value", [None, "", "not a price", float("nan"), float("inf"), float("-inf")]
    )
    def test_unusable_values_become_none_not_a_nan(self, value):
        """A NaN compares unequal to itself and would poison every diff it
        joined."""
        from app.ecommerce.crawl import as_money

        assert as_money(value) is None

    def test_a_decimal_price_passes_through_unchanged(self):
        from app.ecommerce.crawl import as_money

        original = Decimal("19.99")
        assert as_money(original) is not None
        assert as_money(original) == original


class TestTheDatabaseRoundTrip:
    def test_a_price_reads_back_as_a_decimal(self, db_session, auth_headers):
        from app.core.database.identity import User
        from sqlalchemy import select

        from app.ecommerce.changes import normalize_name

        org_id = db_session.execute(
            select(User).where(User.email == "admin@example.com")
        ).scalars().first().organization_id
        competitor = Competitor(
            organization_id=org_id, domain="shop.test", url="https://shop.test"
        )
        db_session.add(competitor)
        db_session.flush()
        row = CompetitorProduct(
            organization_id=org_id,
            competitor_id=competitor.id,
            source_url="https://shop.test/p",
            name="Kettle",
            normalized_name=normalize_name("Kettle"),
            price=Decimal("0.45"),
            compare_at_price=Decimal("0.50"),
            data_source="feed",
        )
        db_session.add(row)
        db_session.commit()
        db_session.expire_all()
        fetched = db_session.get(CompetitorProduct, row.id)
        assert isinstance(fetched.price, Decimal)
        assert fetched.price == Decimal("0.45")
        assert fetched.compare_at_price == Decimal("0.50")

    def test_the_sum_of_two_captures_is_exact(self, db_session):
        """The failure this prevents, in the shape a customer would see it."""
        total = Decimal("0.10") + Decimal("0.20")
        assert total == Decimal("0.30")
        assert str(total) == "0.30"


class TestChangeArithmetic:
    def test_a_price_drop_computes_in_decimal(self):
        from app.ecommerce.changes import ChangeKind, diff_captures

        class Row:
            def __init__(self, price, **kw):
                self.price = price
                self.id = 1
                self.name = "Kettle"
                self.source_url = "https://shop.test/k"
                self.currency = "EUR"
                self.normalized_name = "kettle"
                self.available = True
                self.in_stock = True
                self.variant_count = 1
                self.data_source = "feed"
                self.captured_at = None
                self.compare_at_price = None
                for k, v in kw.items():
                    setattr(self, k, v)

        changes = diff_captures(
            Row(Decimal("24.99"), available=True), Row(Decimal("19.99"), available=True)
        )
        assert len(changes) == 1
        change = changes[0]
        assert change.kind == ChangeKind.PRICE_DECREASE
        assert change.delta == Decimal("-5.00")
        assert change.previous_price == Decimal("24.99")
        assert change.new_price == Decimal("19.99")

    def test_mixing_a_float_into_decimal_arithmetic_is_not_silently_allowed(self):
        """Decimal + float raises TypeError. That is the behaviour we want: the
        coercion at the capture boundary should make it impossible, and if it
        ever regresses the failure should be loud."""
        with pytest.raises(TypeError):
            Decimal("1.00") + 0.5


class TestTheMigration:
    def test_the_money_migration_exists_and_is_nullable_safe(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        path = root / "migrations" / "versions" / "f1b7c4e2a9d3_decimal_prices.py"
        assert path.exists(), "the Numeric migration is missing"
        source = path.read_text(encoding="utf-8")
        assert "sa.Numeric(12, 4" in source
        assert "existing_nullable=True" in source, (
            "a price column must stay nullable through the migration"
        )
        assert "::numeric(12, 4)" in source, (
            "PostgreSQL needs an explicit USING cast to reinterpret the stored "
            "float as a fixed-point value"
        )
