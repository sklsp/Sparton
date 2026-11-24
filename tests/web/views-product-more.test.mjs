// product.js on the DOM fake, second pass: the branch sides views-product.test.mjs leaves unexercised.
// A single reading (with a null-priced sibling) draws one point with a note and grid lines snapped to
// whole steps; a narrow chart host shrinks the plot and thins the week labels; your own line takes the
// chart currency when a reading has none, an unknown match confidence falls back to "possible", and an
// own product without readings is ignored; an already-acknowledged change is not acked again; a missing
// product name, a failed competitor lookup and a jsonld reading all degrade gracefully. Fixtures mirror
// GET /changes/{id}/history in app/api/commerce.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake.
import productView from "../../app/web/views/product.js";
import { t, fmtMoney } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom } from "./dom.mjs";

// The fake Element has no layout; give it a clientWidth the tests can steer so the chart's narrow-host
// branch is reachable. 0 keeps product.js on its 640 default. Each test file runs in its own process,
// so this patch stays local to this file.
let hostWidth = 0;
Object.defineProperty(Object.getPrototypeOf(document.createElement("i")), "clientWidth", {
  configurable: true,
  get() { return hostWidth; },
});

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const tick = () => new Promise((r) => setTimeout(r, 0));

const change = { id: 301, shop_id: 1, competitor_id: 7, kind: "price_decrease", severity: "high", title: "Zwarte brood down", summary: "", competitor: "Bakkerij Jansen", competitor_domain: "bakkerijjansen.nl", product: "Zwarte brood", previous_price: 4.5, new_price: 3.95, currency: "EUR", delta: -0.55, delta_pct: -12.2, evidence_url: null, detected_at: "2026-11-08T08:00:00Z", acknowledged_at: null, vs_you: null };
const point = (captured_at, price, extra = {}) => ({ captured_at, price, compare_at_price: null, in_stock: true, data_source: "html", source_label: "Extracted from the page", source_url: "https://bakkerijjansen.nl/zwarte-brood", ...extra });
const history = (over = {}) => ({ change, own: null, product: "Zwarte brood", competitor: "Bakkerij Jansen", currency: "EUR", points: [point("2026-11-07T08:00:00Z", 4.5), point("2026-11-08T08:00:00Z", 3.95)], ...over });
const competitor = { id: 7, shop_id: 1, domain: "bakkerijjansen.nl", name: "Bakkerij Jansen", data_source: "html" };

function routes(state) {
  return (href, init = {}) => {
    if (href === "/changes/301/history") return state.historyStatus ? json({ detail: state.historyDetail }, state.historyStatus) : json(state.history);
    if (href === "/changes/null/history") return json({ detail: "Change not found" }, 404);
    if (href === "/changes/301/ack" && init.method === "POST") { state.acks += 1; return json({ acknowledged: true }); }
    if (href === "/competitors") return state.competitorsStatus ? json({ detail: "database down" }, state.competitorsStatus) : json({ count: 1, competitors: [competitor] });
    throw new Error(`unexpected fetch ${href}`);
  };
}

async function render(state, hash = "#/product?change=301", hostWidthValue = 0) {
  resetEnv(); resetDom(); resetToasts(); hostWidth = hostWidthValue;
  setFetch(routes(state));
  location.hash = hash;
  const host = document.createElement("main");
  const stop = productView(host);
  for (let i = 0; i < 5; i++) await tick();
  return { host, stop };
}

const base = (over = {}) => ({ history: history(), acks: 0, ...over });

test("a single reading draws one point with a note and grid lines snapped to whole steps", async () => {
  // One priced reading at zero plus an unpriced sibling: the chart falls back to its own scale,
  // snaps both ends onto the step grid, and says there is only one reading.
  const state = base({ history: history({ points: [point("2026-11-08T08:00:00Z", 0), point("2026-11-08T09:00:00Z", null)] }) });
  const { host, stop } = await render(state);
  assert.ok(host.querySelector(".chart-note").textContent.includes(t("pd.chart.one")));
  const dots = host.querySelectorAll("circle");
  assert.equal(dots.length, 1); // the null-priced reading is not plotted
  assert.equal(dots[0].getAttribute("class"), "pt-extracted");
  const rows = host.querySelectorAll("tr").slice(1);
  assert.deepEqual(rows.map((r) => r.querySelector(".num.tnum").textContent), ["\u2014", fmtMoney(0, "EUR")]); // newest first; product.js renders a missing price as U+2014
  stop();
});

// Week labels are the chart-tick texts that read like "wk 38" in both locales; price ticks carry currency instead.
// SVG classes are set with setAttribute, so select on the attribute, as views-product.test.mjs does for .pt-struck.
const weekLabels = (host) => [...host.querySelectorAll('[class="chart-tick"]')].filter((el) => el.textContent.startsWith("wk ")).length;
// The weekly ruling lines have no class of their own; the horizontal price rules are chart-hline.
const weekLines = (host) => [...host.querySelectorAll('[class="chart-grid"] line')].filter((l) => !l.getAttribute("class")).length;
// product.js labels every `every`-th ruled week counting from the first: floor((n - 1) / every) + 1.
const labelledWeeks = (weeks, maxLabels) => { const every = Math.ceil(weeks / maxLabels) || 1; return Math.floor((weeks - 1) / every) + 1; };

test("a narrow chart host shrinks the plot and thins the week labels", async () => {
  // Eight Mondays across two months: a wide host rules every week, a narrow one keeps half. The label count is
  // derived from the weeks actually ruled (the first Monday after the range start), so it holds in any timezone.
  const prices = [4.5, 4.5, 4.35, 4.35, 4.2, 4.2, 4.05, 3.95]; // eight Mondays apart
  const dates = ["2026-09-07", "2026-09-14", "2026-09-21", "2026-09-28", "2026-10-05", "2026-10-12", "2026-10-19", "2026-10-26"];
  const state = base({ history: history({ points: dates.map((d, i) => point(`${d}T08:00:00Z`, prices[i])) }) });
  const narrow = await render(state, "#/product?change=301", 480); // below the 520 breakpoint
  assert.equal(narrow.host.querySelector("svg").getAttribute("height"), "220");
  assert.ok(weekLines(narrow.host) >= 7); // at least one ruled week per month of readings
  assert.equal(weekLabels(narrow.host), labelledWeeks(weekLines(narrow.host), 4)); // every second Monday labelled
  narrow.stop();
  const wide = await render(state, "#/product?change=301", 640); // back to the default width
  assert.equal(wide.host.querySelector("svg").getAttribute("height"), "280");
  assert.equal(weekLabels(wide.host), labelledWeeks(weekLines(wide.host), 8));
  wide.stop();
});

test("your own line takes the chart currency when a reading has none and unknown confidence falls back to possible", async () => {
  const own = { product: "Ons zwart brood", source_url: "https://devries.nl/zwart", method: "title", score: 0.6, confidence: "maybe", points: [{ captured_at: "2026-11-08T08:00:00Z", price: 4.25, in_stock: true, data_source: "feed" }] };
  const { host, stop } = await render(base({ history: history({ own }) }));
  assert.ok(host.querySelector('[class="line-yours"]')); // no currency on the reading -> chart currency counts as a match
  assert.ok(host.querySelector(".chart-legend").textContent.includes(`Ons zwart brood · ${t("pd.match.possible")}`));
  stop();
});

test("an already-acknowledged change is not acknowledged again", async () => {
  const state = base({ history: history({ change: { ...change, acknowledged_at: "2026-11-08T09:30:00Z" } }) });
  const { stop } = await render(state);
  assert.equal(state.acks, 0);
  stop();
});

test("a missing product name and a failed competitor lookup degrade gracefully", async () => {
  // The own product has no readings at all: the chart must skip it without a line or legend entry.
  const state = base({ competitorsStatus: 500, history: history({ product: "", own: { product: "Ons zwart brood", source_url: null, method: "title", score: 0.9, confidence: "likely" } }) });
  const { host, stop } = await render(state);
  assert.equal(host.querySelector("h1.view-title").textContent, t("pd.untitled"));
  assert.equal(host.querySelector("p.view-sub").textContent, "Bakkerij Jansen"); // no source suffix without a competitor
  assert.ok(host.querySelector("svg"));
  assert.equal(host.querySelector('[class="line-yours"]'), null);
  assert.equal(state.acks, 1); // the change itself is still unacknowledged
  stop();
});

test("a jsonld reading counts as exact in the chart and the table", async () => {
  const state = base({ history: history({ points: [point("2026-11-07T08:00:00Z", 4.5), point("2026-11-08T08:00:00Z", 3.95, { data_source: "jsonld" })] }) });
  const { host, stop } = await render(state);
  assert.deepEqual([...host.querySelectorAll("circle")].map((c) => c.getAttribute("class")), ["pt-extracted", "pt-exact"]);
  const rows = host.querySelectorAll("tr").slice(1);
  assert.equal(rows[0].querySelector(".src-tag").textContent, t("src.exact")); // newest first: the jsonld reading
  stop();
});

test("opening without a change id shows the not-found state", async () => {
  const { host, stop } = await render(base(), "#/product");
  assert.ok(host.textContent.includes(t("pd.error")));
  assert.ok(host.textContent.includes(t("pd.notFound")));
  stop();
});
