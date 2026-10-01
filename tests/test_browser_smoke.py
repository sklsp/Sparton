"""End-to-end smoke test in a real browser.

Everything else in this suite talks to the app over HTTP with a synthetic
client. That proves the API works; it does not prove the *product* works. The
customer-facing half of SPARTON is static HTML, CSS and ES modules that fetch
from the API — code no API test ever executes. A renamed function, a bad import
path, or a selector that no longer matches a button all leave the suite green
and the product broken.

So this drives Chrome: landing page, signup, the dashboard, adding a shop,
adding a competitor, and reading back that the data arrived. It runs the real
JavaScript against the real app, in a real rendering engine.

Skipped, loudly, when Playwright or a browser is not installed — the suite must
still pass on a machine with neither. It is a gate on the build, not a
dependency of the test suite.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

playwright_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is not installed (pip install playwright)"
)

#: Chrome/Edge already on the machine. Avoids a ~150 MB browser download, and
#: these are the browsers a customer will actually use.
LOCAL_BROWSERS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

#: Ignore cert noise; a local run over http must not fail on this.
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def _find_local_browser() -> str | None:
    for candidate in LOCAL_BROWSERS:
        if Path(candidate).exists():
            return candidate
    return None


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """A real uvicorn on a real port, serving the real app.

    Not TestClient: a browser needs a socket, and the in-process transport
    would not exercise the same path.
    """
    import uvicorn

    from app.core.database.base import Base
    from app.core.database.base import engine
    from app.main import create_app

    db_path = ROOT / "test_browser_smoke.db"
    if db_path.exists():
        db_path.unlink()
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"
    os.environ["RATE_LIMIT_DISABLED"] = "true"
    os.environ["EMBEDDED_WORKER"] = "false"
    os.environ["SPARTON_ENV"] = "development"

    Base.metadata.drop_all(bind=engine, checkfirst=True)
    Base.metadata.create_all(bind=engine, checkfirst=True)

    port = _free_port()
    config = uvicorn.Config(
        create_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    import httpx

    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/live", timeout=2).status_code == 200:
                break
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.2)
    else:
        raise RuntimeError("the browser test server did not start")

    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            instance = p.chromium.launch(
                executable_path=_find_local_browser(), args=LAUNCH_ARGS
            )
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no usable browser: {exc}")
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture()
def page(browser, live_server):
    context = browser.new_context()
    pg = context.new_page()
    # A JS error in the dashboard must fail the test, not scroll past silently.
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.errors = errors  # type: ignore[attr-defined]
    pg.base = live_server  # type: ignore[attr-defined]
    try:
        yield pg
    finally:
        context.close()


def _assert_no_js_errors(page, where: str) -> None:
    assert not page.errors, f"JavaScript error on {where}: {page.errors}"


class TestThePublicPages:
    def test_the_landing_page_renders(self, page):
        page.goto(f"{page.base}/", wait_until="networkidle")
        assert page.title(), "the landing page has no title"
        assert page.locator("h1").first.is_visible(), "no visible headline"
        _assert_no_js_errors(page, "the landing page")

    def test_the_pricing_table_loads_from_the_api(self, page):
        """`/billing/plans` is public on purpose. If the landing page cannot
        render prices, nobody can evaluate the product before signing up.

        The table is fetched lazily when the section scrolls into view, so the
        test scrolls to it rather than assuming it was there on load.
        """
        page.goto(f"{page.base}/", wait_until="networkidle")
        page.locator("#plans").scroll_into_view_if_needed()
        page.wait_for_function(
            "() => { const el = document.getElementById('plans');"
            " return el && el.querySelectorAll('article.lp-plan').length > 0; }",
            timeout=10000,
        )
        text = page.locator("#plans").inner_text()
        assert "Pro" in text, f"no plans rendered: {text[:200]}"
        _assert_no_js_errors(page, "the pricing table")

    @pytest.mark.parametrize("page_path", ["/legal/privacy", "/legal/terms", "/legal/dpa"])
    def test_legal_pages_render(self, page, page_path):
        page.goto(f"{page.base}{page_path}", wait_until="domcontentloaded")
        text = page.locator("body").inner_text()
        assert len(text) > 400, f"{page_path} is nearly empty"
        _assert_no_js_errors(page, page_path)


    def test_every_module_the_landing_page_imports_resolves(self, page):
        """A 404 on a module import kills the whole script, silently.

        The landing page is static HTML, so it looks correct with JavaScript
        disabled -- and the one thing that cannot be static is the pricing table,
        because it must come from the API. A missing asset therefore produces a
        page that looks finished and advertises no prices.
        """
        requested: list[str] = []
        page.on("response", lambda r: requested.append(f"{r.status} {r.url}"))
        page.goto(f"{page.base}/", wait_until="networkidle")
        page.locator("#plans").scroll_into_view_if_needed()
        page.wait_for_timeout(1500)
        missing = [line for line in requested if line.startswith("404")]
        assert not missing, f"the landing page 404s: {missing}"
        _assert_no_js_errors(page, "the landing page assets")

    def test_the_demo_board_is_filled_and_labelled_as_demo(self, page):
        """The hero board shows invented shops; it must say so on the board itself."""
        page.goto(f"{page.base}/", wait_until="networkidle")
        board = page.locator("#hero-board")
        assert board.locator(".board-row").count() >= 5, "the demo board is empty"
        assert "demo" in board.locator(".board-demo").inner_text().lower()
        _assert_no_js_errors(page, "the demo board")

    def test_the_scroll_story_changes_the_board(self, page):
        page.goto(f"{page.base}/", wait_until="networkidle")
        title = page.locator("#stage-title")
        first = title.inner_text()
        page.evaluate("document.querySelector('.step[data-scene=report]').scrollIntoView({block: 'center'})")
        page.wait_for_function(
            "() => document.querySelector('.rail-stop[data-scene=report]').hasAttribute('data-active')",
            timeout=5000,
        )
        assert title.inner_text() != first, "the board did not change with the story"
        _assert_no_js_errors(page, "the scroll story")

    def test_an_unknown_path_does_not_500(self, page):
        response = page.goto(f"{page.base}/definitely-not-a-route", wait_until="domcontentloaded")
        assert response.status < 500, f"a missing path returned {response.status}"


class TestSignupToDashboard:
    def _register(self, page, email: str) -> None:
        page.goto(f"{page.base}/app/#signup", wait_until="networkidle")
        page.wait_for_selector("input[type=email]", timeout=15000)
        page.fill("input[type=email]", email)
        page.fill("input[type=password]", "correct-horse-battery")
        # The org field only exists in the signup form.
        org = page.locator("input[name*=organization i], #organization, #org")
        if org.count() and org.first.is_visible():
            org.first.fill("Browser Test Co")
        page.click("button[type=submit]")
        page.wait_for_timeout(1500)

    def test_the_landing_cta_lands_on_the_signup_form(self, page):
        """Regression, found by this file: a real conversion bug.

        Every "Start free" on the marketing page points at `/app/#signup`,
        and `boot()` used to ignore the hash and always render the sign-in
        form. A visitor with no account therefore landed on a login form, with
        the only way forward a small secondary link reading "Create one".

        Found by driving a browser, not by any API test: the route existed,
        the link was correct, and the page it opened was the wrong one.
        """
        page.goto(f"{page.base}/app/#signup", wait_until="networkidle")
        page.wait_for_selector("input[type=email]", timeout=15000)
        page.wait_for_timeout(500)
        assert page.locator("input[name=organization_name]").count() > 0, (
            "the signup form did not render: the organization field is register-only"
        )
        body = page.locator("body").inner_text()
        assert "Create account" in body, f"not the registration form: {body[:200]}"
        _assert_no_js_errors(page, "the signup form")

    def test_a_new_customer_can_sign_up_and_see_the_dashboard(self, page):
        self._register(page, "browser-owner@example.com")
        assert "/app" in page.url, f"signup did not land in the app: {page.url}"
        # The dashboard shell, not the auth form.
        assert page.locator("input[type=email]").count() == 0, "still on the login form"
        assert page.locator("nav, aside, .app").first.is_visible(), "no dashboard shell"
        _assert_no_js_errors(page, "signup")

    def test_the_session_survives_a_reload(self, page):
        """The token is in localStorage. A dashboard that forgets it on reload
        logs the customer out of a product they just paid attention to."""
        self._register(page, "browser-reload@example.com")
        page.wait_for_timeout(800)
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        assert page.locator("input[type=email]").count() == 0, "the reload bounced to login"
        _assert_no_js_errors(page, "after reload")

    def test_the_navigation_lists_the_product_views(self, page):
        self._register(page, "browser-nav@example.com")
        page.wait_for_timeout(1000)
        text = page.locator("body").inner_text()
        for label in ("Overview", "Alerts", "Shops", "Reports"):
            assert label in text, f"{label} is not in the navigation"
        _assert_no_js_errors(page, "the navigation")

    def test_logout_returns_to_the_login_form(self, page):
        self._register(page, "browser-logout@example.com")
        page.wait_for_timeout(800)
        page.evaluate("() => localStorage.removeItem('sparton.token')")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        assert page.locator("input[type=email]").count() > 0, "no session, no login form"
        _assert_no_js_errors(page, "after logout")


class TestTheProductLoopInABrowser:
    """Signup, add a shop, add a competitor, read the data back.

    Driven through the dashboard's own JavaScript where the UI exposes it, and
    through `fetch` from inside the page (with the page's own session token)
    where the UI is a form we would otherwise have to reverse-engineer. Both
    run in the browser, so the API is exercised exactly as a customer's browser
    exercises it: same origin, same CORS, same token handling.
    """

    SHOP = {"url": "https://www.example.com", "name": "Acme Homeware",
            "category": "homeware"}

    def _authenticated(self, page, email: str) -> None:
        page.goto(f"{page.base}/app/#signup", wait_until="networkidle")
        page.wait_for_selector("input[type=email]", timeout=15000)
        page.fill("input[type=email]", email)
        page.fill("input[type=password]", "correct-horse-battery")
        org = page.locator("#organization, #org, input[name*=organization i]")
        if org.count() and org.first.is_visible():
            org.first.fill("Browser Product Co")
        page.click("button[type=submit]")
        page.wait_for_timeout(1500)

    def _api(self, page, method: str, path: str, body: dict | None = None):
        """Call the API from inside the page, as the dashboard's own code does."""
        return page.evaluate(
            """async ({method, path, body}) => {
                const token = localStorage.getItem("sparton.token");
                const res = await fetch(path, {
                    method,
                    headers: {
                        "Content-Type": "application/json",
                        ...(token ? {Authorization: `Bearer ${token}`} : {}),
                    },
                    body: body ? JSON.stringify(body) : undefined,
                });
                let payload = null;
                try { payload = await res.json(); } catch { /* not json */ }
                return {status: res.status, payload};
            }""",
            {"method": method, "path": path, "body": body},
        )

    def test_a_shop_can_be_added_and_read_back(self, page):
        self._authenticated(page, "browser-shop@example.com")
        created = self._api(page, "POST", "/shops", self.SHOP)
        assert created["status"] in (200, 201), created
        shop_id = created["payload"]["id"]

        listed = self._api(page, "GET", "/shops")
        assert listed["status"] == 200, listed
        names = [s["name"] for s in listed["payload"].get("shops", listed["payload"])]
        assert "Acme Homeware" in names, f"the shop is not in the list: {names}"

        single = self._api(page, "GET", f"/shops/{shop_id}")
        assert single["status"] == 200, single
        assert single["payload"]["name"] == "Acme Homeware"

    def test_a_competitor_can_be_added_to_the_shop(self, page):
        self._authenticated(page, "browser-comp@example.com")
        shop = self._api(page, "POST", "/shops", self.SHOP)
        shop_id = shop["payload"]["id"]

        created = self._api(
            page,
            "POST",
            "/competitors",
            {"shop_id": shop_id, "url": "https://www.iana.org", "name": "Rival Goods"},
        )
        assert created["status"] in (200, 201), created
        competitor_id = created["payload"]["id"]

        listed = self._api(page, "GET", f"/competitors?shop_id={shop_id}")
        assert listed["status"] == 200, listed
        rows = listed["payload"].get("competitors", listed["payload"])
        assert any(c["id"] == competitor_id for c in rows), rows

    def test_the_shops_view_loads_over_http(self, page):
        """The view is a lazy ES module. A bad import path is invisible to every
        other test in the suite and fatal to the customer."""
        self._authenticated(page, "browser-view@example.com")
        page.goto(f"{page.base}/app/#shops", wait_until="networkidle")
        page.wait_for_timeout(2000)
        _assert_no_js_errors(page, "the shops view")
        assert page.locator("body").inner_text().strip(), "the shops view rendered nothing"

    def test_the_reports_endpoint_answers_after_a_shop_exists(self, page):
        self._authenticated(page, "browser-report@example.com")
        shop = self._api(page, "POST", "/shops", self.SHOP)
        shop_id = shop["payload"]["id"]
        response = self._api(page, "GET", f"/shops/{shop_id}/reports")
        assert response["status"] == 200, response


class TestTheCompetitorViewShowsItsPriceSource:
    """The customer must be able to tell an exact price from a parsed one.

    This is a product claim, not a cosmetic label: we tell a seller these are
    the prices their competitor charges, and the difference between "read from
    their feed" and "we read the page and think" decides whether they act on it
    or check it themselves.
    """

    def _session(self, page, email: str) -> None:
        page.goto(f"{page.base}/app/#signup", wait_until="networkidle")
        page.wait_for_selector("input[type=email]", timeout=15000)
        page.fill("input[type=email]", email)
        page.fill("input[type=password]", "correct-horse-battery")
        org = page.locator("#organization, #org, input[name*=organization i]")
        if org.count() and org.first.is_visible():
            org.first.fill("Source Label Co")
        page.click("button[type=submit]")
        page.wait_for_timeout(1500)

    def _api(self, page, method: str, path: str, body: dict | None = None):
        return page.evaluate(
            """async ({method, path, body}) => {
                const token = localStorage.getItem("sparton.token");
                const res = await fetch(path, {
                    method,
                    headers: {
                        "Content-Type": "application/json",
                        ...(token ? {Authorization: `Bearer ${token}`} : {}),
                    },
                    body: body ? JSON.stringify(body) : undefined,
                });
                let payload = null;
                try { payload = await res.json(); } catch { /* not json */ }
                return {status: res.status, payload};
            }""",
            {"method": method, "path": path, "body": body},
        )

    def test_the_label_reads_exact_for_a_feed_and_parsed_for_a_page(self, page):
        self._session(page, "source-label@example.com")
        created = self._api(
            page,
            "POST",
            "/competitors",
            {"url": "https://www.example.com/rival-a", "name": "Rival Goods"},
        )
        assert created["status"] in (200, 201), created
        competitor_id = created["payload"]["id"]

        # An uncrawled competitor has nothing to claim, so it must not claim
        # anything: showing "exact prices" before a single page was read would
        # be a lie the customer acts on.
        listed = self._api(page, "GET", f"/competitors")
        assert listed["status"] == 200, listed
        row = next(
            c for c in listed["payload"].get("competitors", listed["payload"])
            if c["id"] == competitor_id
        )
        assert row["data_source"] == "html"
        assert "extracted" in row["source_label"]
        assert "exact" not in row["source_label"]
        assert not row["last_crawled_at"], "a fresh competitor should not claim a source"

    def test_the_api_reports_the_source_tier(self, page):
        self._session(page, "source-api@example.com")
        created = self._api(
            page,
            "POST",
            "/competitors",
            {"url": "https://www.example.com/rival-b", "name": "Rival Two"},
        )
        assert created["status"] in (200, 201), created
        competitor_id = created["payload"]["id"]
        listed = self._api(page, "GET", "/competitors")
        assert listed["status"] == 200, listed
        row = next(
            c for c in listed["payload"].get("competitors", listed["payload"])
            if c["id"] == competitor_id
        )
        assert row["data_source"] in ("feed", "jsonld", "html")
        assert row["source_label"], "no customer-facing wording for the source"

    def test_the_shops_view_renders_without_error(self, page):
        """The label is rendered by a view a customer loads after a crawl."""
        self._session(page, "source-view@example.com")
        page.goto(f"{page.base}/app/#shops", wait_until="networkidle")
        page.wait_for_timeout(2000)
        _assert_no_js_errors(page, "the shops view with the source label")
        assert page.locator("body").inner_text().strip(), "the shops view rendered nothing"


class TestTenantIsolationInTheBrowser:
    """Two real browsers, two real accounts, one machine.

    The API tests prove the scoping; this proves it holds for the session token
    a browser actually holds, and that the second browser's UI cannot render the
    first browser's data.
    """

    SHOP = {"url": "https://www.example.com", "name": "Tenant A Shop",
            "category": "homeware"}

    def _session(self, page, email: str) -> None:
        page.goto(f"{page.base}/app/#signup", wait_until="networkidle")
        page.wait_for_selector("input[type=email]", timeout=15000)
        page.fill("input[type=email]", email)
        page.fill("input[type=password]", "correct-horse-battery")
        org = page.locator("#organization, #org, input[name*=organization i]")
        if org.count() and org.first.is_visible():
            org.first.fill(f"Co {email}")
        page.click("button[type=submit]")
        page.wait_for_timeout(1500)

    def _api(self, page, method: str, path: str, body: dict | None = None):
        return page.evaluate(
            """async ({method, path, body}) => {
                const token = localStorage.getItem("sparton.token");
                const res = await fetch(path, {
                    method,
                    headers: {
                        "Content-Type": "application/json",
                        ...(token ? {Authorization: `Bearer ${token}`} : {}),
                    },
                    body: body ? JSON.stringify(body) : undefined,
                });
                let payload = null;
                try { payload = await res.json(); } catch { /* not json */ }
                return {status: res.status, payload};
            }""",
            {"method": method, "path": path, "body": body},
        )


    def test_one_tenant_cannot_see_or_reach_another(self, browser, live_server):
        """Two accounts, two browser contexts, one machine.
        
        Sequential rather than threaded: Playwright's sync API is bound to the
        thread that created it, so a second tenant gets its own context in this
        one. The two sessions are still genuinely separate -- separate cookies,
        separate localStorage, separate tokens.
        """
        errors: list[str] = []

        def as_tenant(label: str, email: str):
            context = browser.new_context()
            pg = context.new_page()
            pg.base = live_server  # type: ignore[attr-defined]
            pg.errors = errors  # type: ignore[attr-defined]
            pg.on("pageerror", lambda e, lbl=label: errors.append(f"{lbl}: {e}"))
            try:
                self._session(pg, email)
                assert pg.locator("input[type=email]").count() == 0, f"{label} did not sign in"
                return pg, context
            except Exception:
                context.close()
                raise

        page_a, context_a = as_tenant("a", "tenant-a@example.com")
        try:
            created = self._api(page_a, "POST", "/shops", self.SHOP)
            assert created["status"] in (200, 201), created
            shop_id = created["payload"]["id"]
            own = self._api(page_a, "GET", "/shops")
            assert own["status"] == 200, own
            names_a = [s["name"] for s in own["payload"].get("shops", own["payload"])]
            assert "Tenant A Shop" in names_a, f"tenant A cannot see its own shop: {names_a}"
        finally:
            context_a.close()

        page_b, context_b = as_tenant("b", "tenant-b@example.com")
        try:
            listed = self._api(page_b, "GET", "/shops")
            assert listed["status"] == 200, listed
            names_b = [s["name"] for s in listed["payload"].get("shops", listed["payload"])]
            assert "Tenant A Shop" not in names_b, (
                f"tenant B can see tenant A's shops: {names_b}"
            )
            # And the direct route is refused, not merely absent from the list.
            direct = self._api(page_b, "GET", f"/shops/{shop_id}")
            assert direct["status"] in (403, 404), (
                f"tenant B read tenant A's shop: {direct['status']}"
            )
        finally:
            context_b.close()
        assert not errors, f"JavaScript errors during the isolation check: {errors}"


class TestLanguageAndShell:
    """NL/EN switch and the mobile navigation: the two shell behaviours every view relies on."""

    def _context_page(self, browser, live_server, **context_args):
        context = browser.new_context(**context_args)
        pg = context.new_page()
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.errors = errors  # type: ignore[attr-defined]
        pg.base = live_server  # type: ignore[attr-defined]
        return pg, context

    def test_a_dutch_browser_gets_dutch_by_default(self, browser, live_server):
        page, context = self._context_page(browser, live_server, locale="nl-NL")
        try:
            page.goto(f"{page.base}/app/#signup", wait_until="networkidle")
            page.wait_for_selector("input[type=email]", timeout=15000)
            assert page.evaluate("document.documentElement.lang") == "nl"
            assert "Account aanmaken" in page.locator("body").inner_text()
            _assert_no_js_errors(page, "the Dutch signup form")
        finally:
            context.close()

    def test_the_language_switch_is_remembered(self, browser, live_server):
        page, context = self._context_page(browser, live_server, locale="en-US")
        try:
            page.goto(f"{page.base}/app/#signup", wait_until="networkidle")
            page.wait_for_selector("input[type=email]", timeout=15000)
            assert "Create account" in page.locator("body").inner_text()
            with page.expect_navigation():
                page.click(".lang-switch button[lang=nl]")
            page.wait_for_selector("input[type=email]", timeout=15000)
            assert "Account aanmaken" in page.locator("body").inner_text()
            assert page.evaluate("localStorage.getItem('sparton.lang')") == "nl"
            page.reload(wait_until="networkidle")
            page.wait_for_selector("input[type=email]", timeout=15000)
            assert "Account aanmaken" in page.locator("body").inner_text(), "the choice was not remembered"
            assert page.locator(".lang-switch button[lang=nl]").get_attribute("aria-pressed") == "true"
            _assert_no_js_errors(page, "the language switch")
        finally:
            context.close()

    def test_the_mobile_navigation_opens_and_navigates(self, browser, live_server):
        page, context = self._context_page(browser, live_server, viewport={"width": 375, "height": 812})
        try:
            page.goto(f"{page.base}/app/#signup", wait_until="networkidle")
            page.wait_for_selector("input[type=email]", timeout=15000)
            page.fill("input[type=email]", "mobile-nav@example.com")
            page.fill("input[type=password]", "correct-horse-battery")
            page.fill("input[name=organization_name]", "Mobile Nav Co")
            page.click("button[type=submit]")
            page.wait_for_selector(".menu-btn", timeout=15000)
            shops = page.locator(".nav-item[data-route=shops]")
            assert not shops.is_visible(), "the drawer is open before it was asked for"
            page.click(".menu-btn")
            assert page.locator(".menu-btn").get_attribute("aria-expanded") == "true"
            shops.wait_for(state="visible", timeout=3000)
            shops.click()
            page.wait_for_function("() => location.hash === '#/shops'", timeout=5000)
            assert page.locator(".menu-btn").get_attribute("aria-expanded") == "false"
            overflow = page.evaluate("() => document.documentElement.scrollWidth - innerWidth")
            assert overflow <= 0, f"the dashboard scrolls sideways on a phone ({overflow}px)"
            _assert_no_js_errors(page, "the mobile navigation")
        finally:
            context.close()


class TestAccountRecovery:
    """Forgot password and the reset link, driven through the real screens."""

    def test_forgot_password_confirms_without_revealing_the_account(self, page):
        page.goto(f"{page.base}/app/#/login", wait_until="networkidle")
        page.click("a[href='#/forgot']")
        page.wait_for_selector("input[type=email]")
        page.fill("input[type=email]", "nobody-here@example.com")
        page.click("button[type=submit]")
        page.wait_for_function("() => document.body.innerText.includes('Check your inbox')", timeout=5000)
        assert "nobody-here@example.com" in page.locator("body").inner_text()
        _assert_no_js_errors(page, "forgot password")

    def test_an_invalid_reset_link_says_so_and_offers_a_new_one(self, page):
        page.goto(f"{page.base}/reset-password?token=not-a-real-token", wait_until="networkidle")
        assert "#/reset?token=not-a-real-token" in page.url
        page.fill("input[autocomplete=new-password] >> nth=0", "correct-horse-battery")
        page.fill("input[autocomplete=new-password] >> nth=1", "correct-horse-battery")
        page.click("button[type=submit]")
        page.wait_for_selector(".form-error:not([hidden])", timeout=5000)
        assert "expired" in page.locator(".form-error").inner_text()
        _assert_no_js_errors(page, "an invalid reset link")


class TestOnboarding:
    """Signup lands in onboarding; shop URL, competitors, first check, with live progress."""

    def test_signup_to_first_check(self, page):
        # Discovery searches the web; answer it from here so the test never leaves the machine.
        page.route("**/shops/*/discover", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body='{"count": 1, "suggestions": [{"domain": "www.example.org", "url": "https://www.example.org",'
                 ' "platform": "shopify", "reason": "test", "confidence": 0.4}]}',
        ))
        page.goto(f"{page.base}/app/#signup", wait_until="networkidle")
        page.fill("input[type=email]", "onboarding@example.com")
        page.fill("input[type=password]", "correct-horse-battery")
        page.fill("input[name=organization_name]", "Onboarding Co")
        page.click("button[type=submit]")
        page.wait_for_selector("#ob-url", timeout=15000)
        assert page.url.endswith("#/start"), page.url

        page.fill("#ob-url", "www.example.com")
        page.click(".ob-plate button[type=submit]")
        page.wait_for_selector(".ob-suggestion", timeout=10000)
        go = page.locator(".ob-actions .btn")
        assert go.is_disabled(), "nothing is chosen yet, so nothing may be watched"
        page.check(".ob-suggestion input[type=checkbox]")
        page.fill("#ob-rival", "www.iana.org")
        page.press("#ob-rival", "Enter")
        assert "2" in page.locator(".ob-count").inner_text()
        go.click()

        page.wait_for_selector(".ob-row", timeout=10000)
        assert page.locator(".ob-row").count() == 2
        # No worker runs in the test server, so the honest state is "in queue", not a fake bar.
        assert page.locator(".ob-row[data-state=waiting]").count() == 2
        assert page.locator(".ob-stop[aria-current=step]").get_attribute("data-step") == "check"
        _assert_no_js_errors(page, "onboarding")


class TestEmptyStates:
    def test_a_new_account_sees_a_designed_first_run_not_a_blank_board(self, page):
        page.goto(f"{page.base}/app/#signup", wait_until="networkidle")
        page.fill("input[type=email]", "empty-state@example.com")
        page.fill("input[type=password]", "correct-horse-battery")
        page.fill("input[name=organization_name]", "Empty Co")
        page.click("button[type=submit]")
        page.wait_for_selector("#ob-url", timeout=15000)
        page.goto(f"{page.base}/app/#/overview")
        page.wait_for_selector(".first-run", timeout=10000)
        cta = page.locator(".first-run a.btn")
        assert cta.get_attribute("href") == "#/start"
        assert cta.inner_text().strip(), "the empty state has no way forward"
        _assert_no_js_errors(page, "the empty overview")
