"""The weekly report email: right numbers, both languages, and readable in real inboxes
(600 px wide, images off, dark mode)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.core.report_email import DOWN, INK, UP, _cells, _date, _money, _pct, render_report_email

REPORT: dict[str, Any] = {
    "period_start": "2026-09-24T00:00:00",
    "period_end": "2026-10-01T00:00:00",
    "facts": {
        "shop": {"name": "Acme <Homeware>"},
        "total_changes": 3,
        "by_kind": {"price_decrease": 1, "price_increase": 1},
        "competitors": [{"id": 1}, {"id": 2}],
    },
}
CHANGES: list[dict[str, Any]] = [
    {"kind": "price_decrease", "competitor": "Kade & Co", "product": "Leather belt", "previous_price": 34,
     "new_price": 27.5, "delta_pct": -19.1, "evidence_url": "https://kade.example/products/belt"},
    {"kind": "price_increase", "competitor": "Noord Supply", "product": "Beard oil", "previous_price": 1018.95,
     "new_price": 1121.5, "delta_pct": 10.1, "evidence_url": "https://noord.example/products/oil"},
    {"kind": "out_of_stock", "competitor": "Atelier Vos", "product": "Wool scarf", "new_price": 59,
     "evidence_url": "https://vos.example/products/scarf"},
]


class TestContent:
    def test_english_subject_counts_and_money(self):
        subject, text, html = render_report_email(REPORT, CHANGES, lang="en", report_url="https://app.example/app/#/reports")
        assert subject == "Week 40: 3 changes at your competitors"
        assert "€34.00 → €27.50 −19%" in text
        assert "€1,121.50" in html
        assert "+10%" in html
        assert "1 price cut ·" in text and "2 competitors watched" in text

    def test_dutch_uses_dutch_words_and_decimal_commas(self):
        subject, text, html = render_report_email(REPORT, CHANGES, lang="nl")
        assert subject == "Week 40: 3 veranderingen bij je concurrenten"
        assert "€ 27,50" in text and "€ 1.121,50" in html
        assert "uitverkocht" in html and 'lang="nl"' in html

    def test_every_change_links_to_its_evidence(self):
        _, text, html = render_report_email(REPORT, CHANGES)
        for change in CHANGES:
            assert change["evidence_url"] in text and change["evidence_url"] in html

    def test_shop_names_are_escaped(self):
        _, _, html = render_report_email(REPORT, CHANGES)
        assert "Acme &lt;Homeware&gt;" in html and "<Homeware>" not in html

    def test_a_quiet_week_says_so(self):
        quiet = {**REPORT, "facts": {**REPORT["facts"], "total_changes": 0, "by_kind": {}}}
        subject, text, _ = render_report_email(quiet, [], lang="en")
        assert "nothing moved" in subject
        assert "Nothing moved this week" in text

    def test_no_images_so_nothing_is_lost_with_images_off(self):
        _, _, html = render_report_email(REPORT, CHANGES)
        assert "<img" not in html and "background-image" not in html


class TestDate:
    def test_a_datetime_is_used_as_is(self):
        moment = datetime(2026, 10, 1)
        assert _date(moment) is moment

    def test_an_iso_string_is_parsed(self):
        assert _date("2026-09-24T00:00:00") == datetime(2026, 9, 24)

    @pytest.mark.parametrize("bad", ["not a date", "", None, 12345])
    def test_bad_input_gives_none(self, bad):
        assert _date(bad) is None


class TestMoney:
    @pytest.mark.parametrize(
        ("value", "lang", "expected"),
        [
            (None, "en", ""),
            (None, "nl", ""),
            (0, "en", "€0.00"),
            (0, "nl", "€ 0,00"),
            (34, "en", "€34.00"),
            (27.5, "en", "€27.50"),
            (1121.95, "en", "€1,121.95"),
            (27.5, "nl", "€ 27,50"),
            (1121.95, "nl", "€ 1.121,95"),
        ],
    )
    def test_money_is_formatted_per_language(self, value, lang, expected):
        assert _money(value, lang) == expected


class TestPct:
    def test_a_rise_gets_a_plus_sign(self):
        assert _pct({"delta_pct": 10.1}) == "+10%"

    def test_a_cut_uses_the_typographic_minus(self):
        assert _pct({"delta_pct": -19.1}) == "−19%"

    def test_no_movement_is_shown_as_minus_zero(self):
        assert _pct({"delta_pct": 0}) == "−0%"

    def test_a_missing_delta_is_computed_from_the_prices(self):
        assert _pct({"previous_price": 100, "new_price": 110}) == "+10%"

    def test_no_delta_and_no_prices_gives_an_empty_string(self):
        assert _pct({}) == ""


class TestCells:
    def test_a_price_cut_shows_both_prices_the_drop_and_red(self):
        was, now, delta, colour = _cells(
            {"kind": "price_decrease", "previous_price": 34, "new_price": 27.5, "delta_pct": -19.1}, "en")
        assert (was, now, delta) == ("€34.00", "€27.50", "−19%")
        assert colour is DOWN

    def test_a_price_rise_is_green(self):
        _, _, _, colour = _cells(
            {"kind": "price_increase", "previous_price": 10, "new_price": 12, "delta_pct": 20}, "en")
        assert colour is UP

    @pytest.mark.parametrize(
        ("kind", "word"),
        [
            ("out_of_stock", "sold out"),
            ("back_in_stock", "back in stock"),
            ("new_product", "new"),
            ("removed_product", "removed"),
        ],
    )
    def test_a_non_price_kind_shows_its_word(self, kind, word):
        was, now, delta, colour = _cells({"kind": kind, "new_price": 59}, "en")
        assert (was, now, delta) == ("", "€59.00", word)
        assert colour is INK

    def test_the_words_are_translated(self):
        _, _, soldout, _ = _cells({"kind": "out_of_stock"}, "nl")
        assert soldout == "uitverkocht"

    def test_an_unknown_kind_has_no_word(self):
        was, now, delta, _ = _cells({"kind": "mystery", "new_price": 5}, "en")
        assert (was, now, delta) == ("", "€5.00", "")


playwright_api = pytest.importorskip("playwright.sync_api")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("lang", ["en", "nl"])
def test_the_email_fits_600px_in_light_and_dark(lang, scheme):
    _, _, html = render_report_email(REPORT, CHANGES, lang=lang, report_url="https://app.example/app/#/reports")
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=CHROME if Path(CHROME).exists() else None)
        except Exception as exc:
            pytest.skip(f"no usable browser: {exc}")
        try:
            # Images off, like many mail clients by default.
            context = browser.new_context(viewport={"width": 600, "height": 900}, color_scheme=scheme)
            page = context.new_page()
            page.route(
                "**/*",
                lambda route: route.abort() if route.request.resource_type == "image" else route.continue_(),
            )
            page.set_content(html)
            overflow = page.evaluate("() => document.documentElement.scrollWidth - innerWidth")
            assert overflow <= 0, f"the email scrolls sideways at 600px ({overflow}px)"
            assert page.locator("h1").inner_text().strip()
            out = Path(__file__).resolve().parent.parent / "qa_shots"
            if out.is_dir():
                page.screenshot(path=str(out / f"email-{lang}-{scheme}.png"), full_page=True)
        finally:
            browser.close()
