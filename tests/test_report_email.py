"""The weekly report email: right numbers, both languages, and readable in real inboxes
(600 px wide, images off, dark mode)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.report_email import render_report_email

REPORT = {
    "period_start": "2026-09-24T00:00:00",
    "period_end": "2026-10-01T00:00:00",
    "facts": {
        "shop": {"name": "Acme <Homeware>"},
        "total_changes": 3,
        "by_kind": {"price_decrease": 1, "price_increase": 1},
        "competitors": [{"id": 1}, {"id": 2}],
    },
}
CHANGES = [
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


playwright_api = pytest.importorskip("playwright.sync_api")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("lang", ["en", "nl"])
def test_the_email_fits_600px_in_light_and_dark(lang, scheme):
    _, _, html = render_report_email(REPORT, CHANGES, lang=lang, report_url="https://app.example/app/#/reports")
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=CHROME if Path(CHROME).exists() else None)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no usable browser: {exc}")
        try:
            # Images off, like many mail clients by default.
            context = browser.new_context(viewport={"width": 600, "height": 900}, color_scheme=scheme)
            page = context.new_page()
            page.route("**/*", lambda route: route.abort() if route.request.resource_type == "image" else route.continue_())
            page.set_content(html)
            overflow = page.evaluate("() => document.documentElement.scrollWidth - innerWidth")
            assert overflow <= 0, f"the email scrolls sideways at 600px ({overflow}px)"
            assert page.locator("h1").inner_text().strip()
            out = Path(__file__).resolve().parent.parent / "qa_shots"
            if out.is_dir():
                page.screenshot(path=str(out / f"email-{lang}-{scheme}.png"), full_page=True)
        finally:
            browser.close()
