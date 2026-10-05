"""Screenshot baselines for the five main pages.

Each page is rendered in real Chrome at 1440x900 with reduced motion and a frozen clock, and
compared with `tests/baselines/<platform>/<name>.png`. Fonts render differently per OS, so
baselines are per platform; a missing baseline is recorded and the test skips, so the first run
on a new machine sets it. To accept an intended change, delete the PNG and run again.

The tolerance allows anti-aliasing noise, not layout: a moved block or a missing section fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.test_browser_smoke import browser, live_server  # noqa: F401  (shared fixtures)

Image = pytest.importorskip("PIL.Image", reason="Pillow is needed to compare screenshots")
ImageChops = pytest.importorskip("PIL.ImageChops")

BASELINES = Path(__file__).resolve().parent / "baselines" / sys.platform
#: Share of pixels allowed to differ noticeably before a page counts as changed.
MAX_CHANGED = 0.01
FROZEN = "2026-10-01T10:00:00+02:00"

PAGES = [
    ("landing", "/", None),
    ("pricing", "/pricing", None),
    ("compare", "/vs/prisync", None),
    ("signup", "/app/#/signup", None),
    ("overview", "/app/#/overview", "session"),
]


def _changed_share(a, b) -> float:
    if a.size != b.size:
        return 1.0
    diff = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L")
    noisy = sum(diff.histogram()[24:])  # ignore sub-10% channel differences (anti-aliasing)
    return noisy / (a.size[0] * a.size[1])


@pytest.mark.parametrize("name, path, needs", PAGES)
def test_page_matches_its_baseline(browser, live_server, name, path, needs, tmp_path):  # noqa: F811
    context = browser.new_context(viewport={"width": 1440, "height": 900}, reduced_motion="reduce", locale="en-US")
    page = context.new_page()
    try:
        page.clock.set_fixed_time(FROZEN)
        page.goto(f"{live_server}/", wait_until="domcontentloaded")
        if needs == "session":
            res = page.request.post(f"{live_server}/auth/register", data={
                "email": "baseline@example.com", "password": "correct-horse-battery",
                "organization_name": "Baseline Co"})
            if res.status == 409:
                res = page.request.post(f"{live_server}/auth/login", data={
                    "email": "baseline@example.com", "password": "correct-horse-battery"})
            page.evaluate("t => localStorage.setItem('sparton.token', t)", res.json()["token"])
        page.goto("about:blank")
        page.goto(f"{live_server}{path}", wait_until="networkidle")
        page.wait_for_timeout(600)
        shot = tmp_path / f"{name}.png"
        page.screenshot(path=str(shot))
    finally:
        context.close()

    baseline = BASELINES / f"{name}.png"
    if not baseline.exists():
        BASELINES.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(shot.read_bytes())
        pytest.skip(f"recorded a new baseline: {baseline.name}")
    share = _changed_share(Image.open(shot), Image.open(baseline))
    if share > MAX_CHANGED:
        failed = baseline.with_name(f"{name}.actual.png")
        failed.write_bytes(shot.read_bytes())
        pytest.fail(f"{name} differs from its baseline in {share:.1%} of pixels; see {failed.name}")
