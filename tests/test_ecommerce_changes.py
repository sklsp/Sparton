"""The change-detection rules, branch by branch.

``test_ecommerce.py`` drives these through a full crawl and pins the main loop;
this file tests each rule directly so a regression in one threshold or message
is caught where it lives rather than somewhere in the pipeline. It covers the
branches the end-to-end loop never reaches: LOW-severity moves, rounding jitter
at the epsilon boundary, back-in-stock (with and without a price), the
empty-name guard in ``latest_capture_by_name``, ``is_new``, and the
duplicate-observation skip in ``record_changes``.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database.ecommerce_models import (
    ChangeEvent,
    ChangeKind,
    ChangeSeverity,
    Competitor,
    CompetitorProduct,
)
from app.core.database.models import utcnow
from app.ecommerce.changes import (
    DetectedChange,
    diff_captures,
    is_new,
    latest_capture_by_name,
    money,
    new_product_change,
    price_severity,
    record_changes,
    removed_product_change,
)


def capture(
    *,
    price: Decimal | None = None,
    in_stock: bool = True,
    availability: str = "InStock",
    name: str = "Kettle",
    url: str = "https://shop.test/kettle",
    captured_at=None,
) -> CompetitorProduct:
    """An unsaved capture row. ``diff_captures`` is a pure function of its rows."""
    return CompetitorProduct(
        competitor_id=1,
        source_url=url,
        name=name,
        normalized_name=name.lower(),
        price=price,
        availability=availability,
        in_stock=in_stock,
        captured_at=captured_at or utcnow(),
    )


class TestPriceSeverity:
    """The thresholds a customer sees as 'urgent' vs 'noted'."""

    @pytest.mark.parametrize("pct", [Decimal("20"), Decimal("35.5")])
    def test_a_move_of_twenty_percent_or_more_is_high(self, pct):
        assert price_severity(pct) == ChangeSeverity.HIGH

    def test_negative_moves_are_judged_by_their_magnitude(self):
        # A 30% cut is as urgent as a 30% hike.
        assert price_severity(Decimal("-30")) == ChangeSeverity.HIGH

    @pytest.mark.parametrize("pct", [Decimal("10"), Decimal("19.9")])
    def test_a_move_between_ten_and_twenty_percent_is_medium(self, pct):
        assert price_severity(pct) == ChangeSeverity.MEDIUM

    def test_a_small_meaningful_move_is_low(self):
        # 4% is real (above the epsilon), but it does not need an urgent badge.
        assert price_severity(Decimal("4")) == ChangeSeverity.LOW


class TestMoney:
    def test_known_currencies_use_their_symbol(self):
        assert money(Decimal("27.50"), "EUR") == "€27.50"

    def test_thousands_are_grouped(self):
        assert money(Decimal("1234.56"), "USD") == "$1,234.56"

    def test_unknown_currencies_fall_back_to_the_code(self):
        # No symbol-table entry: the code goes after the number instead.
        assert money(Decimal("9.99"), "SEK") == "9.99 SEK"

    def test_a_missing_price_reads_as_unknown_not_zero(self):
        # €0.00 would claim the product is free; 'unknown' says what we know.
        assert money(None, "EUR") == "unknown"


class TestDiffCaptures:
    def test_back_in_stock_is_detected(self):
        previous = capture(in_stock=False, availability="OutOfStock", price=Decimal("19.99"))
        current = capture(in_stock=True, availability="InStock", price=Decimal("19.99"))

        changes = diff_captures(previous, current)

        assert [c.kind for c in changes] == [ChangeKind.BACK_IN_STOCK]
        assert changes[0].severity == ChangeSeverity.LOW
        assert "back in stock" in changes[0].title

    def test_back_in_stock_without_a_price_says_unknown(self):
        # The page came back with availability but no price: the summary must
        # not invent a number.
        previous = capture(in_stock=False, availability="OutOfStock", price=Decimal("19.99"))
        current = capture(in_stock=True, availability="InStock", price=None)

        changes = diff_captures(previous, current)

        assert [c.kind for c in changes] == [ChangeKind.BACK_IN_STOCK]
        assert changes[0].summary == "Kettle is available again at unknown."

    def test_a_small_price_move_is_low_severity(self):
        # 24.99 -> 23.99 is a real cut (~4%) but not an urgent one.
        previous = capture(price=Decimal("24.99"))
        current = capture(price=Decimal("23.99"))

        changes = diff_captures(previous, current)

        assert [c.kind for c in changes] == [ChangeKind.PRICE_DECREASE]
        assert changes[0].severity == ChangeSeverity.LOW
        assert changes[0].delta == Decimal("-1.00")

    def test_rounding_jitter_below_both_epsilons_is_ignored(self):
        # 100.00 -> 100.49: under 1% and under €0.50, so it is noise.
        previous = capture(price=Decimal("100.00"))
        current = capture(price=Decimal("100.49"))

        assert diff_captures(previous, current) == []

    def test_a_move_above_the_pct_epsilon_but_below_the_abs_one_is_reported(self):
        # 30.00 -> 30.45 is only €0.45 off the shelf price, but on a cheap
        # product that is +1.5%, past the 1% floor.
        previous = capture(price=Decimal("30.00"))
        current = capture(price=Decimal("30.45"))

        changes = diff_captures(previous, current)

        assert [c.kind for c in changes] == [ChangeKind.PRICE_INCREASE]
        assert changes[0].severity == ChangeSeverity.LOW

    def test_a_move_below_the_pct_epsilon_but_above_the_abs_one_is_reported(self):
        # 200.00 -> 199.00 is only -0.5%, but it is a real €1.00 off the shelf price.
        previous = capture(price=Decimal("200.00"))
        current = capture(price=Decimal("199.00"))

        changes = diff_captures(previous, current)

        assert [c.kind for c in changes] == [ChangeKind.PRICE_DECREASE]
        assert changes[0].severity == ChangeSeverity.LOW

    def test_a_price_appearing_where_there_was_none_is_not_a_change(self):
        # A first capture with a price is not "a drop from nothing".
        previous = capture(price=None)
        current = capture(price=Decimal("19.99"))

        assert diff_captures(previous, current) == []


class TestNewAndRemovedProducts:
    def test_new_product_change_reports_the_price(self):
        rival = Competitor(domain="shop.test", name="Claybarn")
        change = new_product_change(capture(price=Decimal("19.99")), competitor=rival)

        assert change.kind == ChangeKind.NEW_PRODUCT
        assert change.severity == ChangeSeverity.LOW
        assert change.title == "Claybarn listed a new product: Kettle"
        assert change.summary == "Kettle is new at Claybarn at €19.99."

    def test_new_product_without_a_price_does_not_invent_one(self):
        rival = Competitor(domain="shop.test", name="Claybarn")
        change = new_product_change(capture(price=None), competitor=rival)

        assert change.kind == ChangeKind.NEW_PRODUCT
        assert change.new_price is None
        assert "€" not in change.summary

    def test_removed_product_change_keeps_the_last_known_price_as_evidence(self):
        rival = Competitor(domain="shop.test", name="Claybarn")
        change = removed_product_change(capture(price=Decimal("19.99")), competitor=rival)

        assert change.kind == ChangeKind.REMOVED_PRODUCT
        assert change.severity == ChangeSeverity.MEDIUM
        assert change.previous_price == Decimal("19.99")
        assert change.new_price is None
        assert "no longer lists" in change.title


class TestIsNew:
    def test_a_product_is_new_only_when_there_was_no_previous_capture(self):
        row = capture()

        assert is_new(None, row) is True
        assert is_new(row, row) is False


@pytest.fixture()
def rival(db_session) -> Competitor:
    competitor = Competitor(organization_id=None, domain="shop.test", url="https://shop.test")
    db_session.add(competitor)
    db_session.flush()
    return competitor


class TestDatabaseHelpers:
    def test_latest_capture_by_name_with_an_empty_name_returns_none(self, db_session, rival):
        # The removal path passes "" as a name; that must not match every row.
        seed = CompetitorProduct(
            organization_id=None,
            competitor_id=rival.id,
            source_url="https://shop.test/kettle",
            name="Kettle",
            normalized_name="kettle",
            price=Decimal("19.99"),
            captured_at=utcnow() - timedelta(hours=1),
        )
        db_session.add(seed)
        db_session.commit()

        before = utcnow() + timedelta(days=1)
        assert latest_capture_by_name(db_session, rival.id, "", before) is None
        # ...and the same query with a real name still finds the row.
        found = latest_capture_by_name(db_session, rival.id, "kettle", before)
        assert found is not None and found.id == seed.id

    def test_record_changes_skips_an_already_recorded_observation(self, db_session, rival):
        # Two crawls racing on the same move must produce one alert, not two.
        row = CompetitorProduct(
            organization_id=None,
            competitor_id=rival.id,
            source_url="https://shop.test/kettle",
            name="Kettle",
            normalized_name="kettle",
            price=Decimal("19.99"),
            captured_at=utcnow() - timedelta(hours=1),
        )
        db_session.add(row)
        db_session.commit()

        change = DetectedChange(
            kind=ChangeKind.PRICE_DECREASE,
            severity=ChangeSeverity.MEDIUM,
            product_name="Kettle",
            source_url=row.source_url,
            previous_price=Decimal("24.99"),
            new_price=Decimal("19.99"),
            currency="EUR",
            competitor_product_id=row.id,
            title="shop.test cut the price of Kettle by 20%",
            summary="Kettle moved from €24.99 to €19.99 (down €5.00).",
        )

        first = record_changes(
            db_session,
            organization_id=None,
            shop_id=None,
            competitor_id=rival.id,
            changes=[change],
            detected_at=utcnow(),
        )
        assert len(first) == 1

        again = record_changes(
            db_session,
            organization_id=None,
            shop_id=None,
            competitor_id=rival.id,
            changes=[change],
            detected_at=utcnow() + timedelta(seconds=5),
        )
        assert again == []
        events = db_session.execute(select(ChangeEvent)).scalars().all()
        assert len(events) == 1
