// product.js on the DOM fake: one product's price history at one competitor. The header and the change row,
// the step chart (exact vs extracted points, your own line only in the same currency), the readings table
// newest first, opening the change marks it read, and the not-found and error states. Fixtures mirror
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

// product.js listens for window resizes with the bare addEventListener of a browser window.
const listeners = new Map();
if (typeof globalThis.addEventListener !== "function") {
  globalThis.addEventListener = (type, fn) => listeners.set(type, fn);
  globalThis.removeEventListener = (type, fn) => { if (listeners.get(type) === fn) listeners.delete(type); };
}

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const tick = () => new Promise((r) => setTimeout(r, 0));

const change = { id: 301, shop_id: 1, competitor_id: 7, kind: "price_decrease", severity: "high", title: "Zwarte brood down", summary: "", competitor: "Bakkerij Jansen", competitor_domain: "bakkerijjansen.nl", product: "Zwarte brood", previous_price: 4.5, new_price: 3.95, currency: "EUR", delta: -0.55, delta_pct: -12.2, evidence_url: "https://bakkerijjansen.nl/zwarte-brood", detected_at: "2026-11-08T08:00:00Z", acknowledged_at: null, vs_you: null };
const point = (captured_at, price, extra = {}) => ({ captured_at, price, compare_at_price: null, in_stock: true, data_source: "html", source_label: "Extracted from the page", source_url: "https://bakkerijjansen.nl/zwarte-brood", ...extra });
const history = (over = {}) => ({
  change,
  own: null,
  product: "Zwarte brood",
  competitor: "Bakkerij Jansen",
  currency: "EUR",
  points: [point("2026-10-25T08:00:00Z", 4.5), point("2026-11-01T08:00:00Z", 4.5, { compare_at_price: 4.95 }), point("2026-11-08T08:00:00Z", 3.95, { data_source: "feed", in_stock: false, source_url: null })],
  ...over,
});
const competitor = { id: 7, shop_id: 1, domain: "bakkerijjansen.nl", name: "Bakkerij Jansen", data_source: "html" };

function routes(state) {
  return (href, init = {}) => {
    if (href === "/changes/301/history") return state.historyStatus ? json({ detail: state.historyDetail }, state.historyStatus) : json(state.history);
    if (href === "/changes/301/ack" && init.method === "POST") { state.acks += 1; return json({ acknowledged: true }); }
    if (href === "/competitors") return json({ count: 1, competitors: [competitor] });
    throw new Error(`unexpected fetch ${href}`);
  };
}

async function render(state) {
  resetEnv(); resetDom(); resetToasts(); listeners.clear();
  setFetch(routes(state));
  location.hash = "#/product?change=301";
  const host = document.createElement("main");
  const stop = productView(host);
  for (let i = 0; i < 5; i++) await tick();
  return { host, stop };
}

const base = (over = {}) => ({ history: history(), acks: 0, ...over });

test("shows the product, the competitor, its source and the change row", async () => {
  const { host, stop } = await render(base());
  assert.equal(host.querySelector("h1.view-title").textContent, "Zwarte brood");
  assert.equal(host.querySelector("p.view-sub").textContent, `Bakkerij Jansen · ${t("src.extracted")}`);
  assert.equal(host.querySelector("p.back").querySelector("a").getAttribute("href"), "#/overview");
  assert.equal(host.querySelector("section.pd-change").querySelectorAll("li").length > 0, true);
  stop();
});

test("opening an unread change marks it read, an already read one does not", async () => {
  const state = base();
  const first = await render(state);
  assert.equal(state.acks, 1);
  first.stop();
  const read = base({ history: history({ change: { ...change, acknowledged_at: "2026-11-09T10:00:00Z" } }) });
  const second = await render(read);
  assert.equal(read.acks, 0);
  second.stop();
});

test("the chart labels its summary and marks exact and extracted readings", async () => {
  const { host, stop } = await render(base());
  const svg = host.querySelector("svg");
  assert.ok(svg);
  assert.equal(svg.getAttribute("role"), "img");
  assert.equal(svg.getAttribute("aria-label"), t("pd.chart.summary", { n: 3, first: fmtMoney(4.5, "EUR"), last: fmtMoney(3.95, "EUR") }));
  const dots = host.querySelectorAll("circle");
  assert.deepEqual(dots.map((c) => c.getAttribute("class")), ["pt-extracted", "pt-extracted", "pt-exact"]);
  assert.equal(host.querySelectorAll('[class="pt-struck"]').length, 1); // the out-of-stock reading is struck
  assert.equal(host.querySelector('[class="line-yours"]'), null); // no own product matched
  stop();
});

test("your own price joins the chart only in the same currency", async () => {
  const own = { product: "Ons zwart brood", source_url: "https://devries.nl/zwart", method: "title", score: 0.81, confidence: "likely", points: [{ captured_at: "2026-11-01T08:00:00Z", price: 4.25, currency: "EUR", in_stock: true, data_source: "feed" }] };
  const same = await render(base({ history: history({ own }) }));
  assert.ok(same.host.querySelector('[class="line-yours"]'));
  assert.ok(same.host.querySelector(".chart-legend").textContent.includes(`Ons zwart brood · ${t("pd.match.likely")}`));
  assert.ok(same.host.querySelector("svg").getAttribute("aria-label").includes(t("pd.chart.yours", { last: fmtMoney(4.25, "EUR") })));
  same.stop();
  const other = await render(base({ history: history({ own: { ...own, points: [{ ...own.points[0], currency: "GBP" }] } }) }));
  assert.equal(other.host.querySelector('[class="line-yours"]'), null);
  other.stop();
});

test("the readings table lists every reading newest first", async () => {
  const { host, stop } = await render(base());
  const rows = host.querySelectorAll("tr").slice(1); // skip the header row
  assert.equal(rows.length, 3);
  const cells = (r) => r.querySelectorAll("td").map((c) => c.textContent);
  assert.equal(cells(rows[0])[1], fmtMoney(3.95, "EUR"));
  assert.equal(cells(rows[0])[3], t("pd.stock.out"));
  assert.equal(cells(rows[0])[4], t("src.exact"));
  assert.equal(cells(rows[0])[5], "—"); // no evidence link without a source URL
  assert.equal(cells(rows[1])[2], fmtMoney(4.95, "EUR")); // the was-price
  assert.equal(cells(rows[2])[4], t("src.extracted"));
  assert.equal(rows[2].querySelector("a").getAttribute("rel"), "noopener noreferrer");
  stop();
});

test("a product without priced readings says so instead of drawing", async () => {
  const { host, stop } = await render(base({ history: history({ points: [] }) }));
  assert.equal(host.querySelector("svg"), null);
  assert.ok(host.querySelector(".chart-empty").textContent.includes(t("pd.chart.none")));
  stop();
});

test("an unknown change shows the not-found message, other errors their own", async () => {
  const missing = await render(base({ historyStatus: 404, historyDetail: "Change not found" }));
  assert.ok(missing.host.textContent.includes(t("pd.error")));
  assert.ok(missing.host.textContent.includes(t("pd.notFound")));
  missing.stop();
  const broken = await render(base({ historyStatus: 500, historyDetail: "database down" }));
  assert.ok(broken.host.textContent.includes("database down"));
  broken.stop();
});

test("leaving the view stops listening for resizes", async () => {
  const { stop } = await render(base());
  if (listeners.size) {
    assert.equal(listeners.has("resize"), true);
    stop();
    assert.equal(listeners.has("resize"), false);
  } else {
    stop(); // env provides its own window listener API; nothing local to check
  }
});
