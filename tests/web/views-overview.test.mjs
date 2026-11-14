// overview.js and its board-view helpers on the DOM fake: the week's changes as rows,
// the start prompt for an empty account, a quiet week that still shows the last moves,
// and the error state with a working retry. Fixtures mirror the API shapes built in
// app/ecommerce/commerce.py (_shop_dict, _competitor_dict) and reports.py (report_to_dict).
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import * as ov from "../../app/web/views/overview.js";
import { changeRow, isoWeek, weekLabel, boardLegend } from "../../app/web/views/board-view.js";
import { t, fmtDate, fmtMoney } from "../../app/web/i18n.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// The shop and its two competitors: one html-sourced (the hatched mark), one api-sourced (exact).
const shop = { id: 1, name: "Bakkerij De Vries", url: "https://devries.nl", domain: "devries.nl", platform: "shopify", category: "bakery", currency: "EUR", crawl_frequency_hours: 24, is_active: true, competitor_count: 2, last_crawled_at: "2026-11-08T09:30:00Z", next_crawl_at: "2026-11-15T09:30:00Z", last_status: "COMPLETED", created_at: "2026-09-01T10:00:00Z" };
const compHtml = { id: 7, shop_id: 1, domain: "bakkerijjansen.nl", name: "Bakkerij Jansen", url: "https://bakkerijjansen.nl", platform: "woocommerce", discovery_method: "search", confidence: 0.92, is_active: true, product_count: 48, last_crawled_at: "2026-11-08T09:30:00Z", last_status: "COMPLETED", data_source: "html", source_label: "Extracted from the page", created_at: "2026-09-02T10:00:00Z" };
const compFeed = { id: 8, shop_id: 1, domain: "broodhuis.nl", name: "Broodhuis", url: "https://broodhuis.nl", platform: "shopify", discovery_method: "search", confidence: 0.97, is_active: true, product_count: 64, last_crawled_at: "2026-11-08T09:35:00Z", last_status: "FAILED", data_source: "api", source_label: "Exact", created_at: "2026-09-02T10:05:00Z" };

// Three changes this week, as _change_dict builds them (vs_you null: no match found).
const cut = { id: 101, shop_id: 1, competitor_id: 7, kind: "price_decrease", severity: "high", title: "Zwarte brood down", summary: "", competitor: "Bakkerij Jansen", competitor_domain: "bakkerijjansen.nl", product: "Zwarte brood", previous_price: 4.5, new_price: 3.95, currency: "EUR", delta: -0.55, delta_pct: -12.2, evidence_url: "https://bakkerijjansen.nl/zwarte-brood", detected_at: "2026-11-12T08:00:00Z", acknowledged_at: null, vs_you: null };
const soldOut = { id: 102, shop_id: 1, competitor_id: 8, kind: "out_of_stock", severity: "medium", title: "Vlaams brood gone", summary: "", competitor: "Broodhuis", competitor_domain: "broodhuis.nl", product: "Vlaams brood", previous_price: null, new_price: 5.2, currency: "EUR", delta: null, delta_pct: null, evidence_url: null, detected_at: "2026-11-11T08:00:00Z", acknowledged_at: null, vs_you: null };
const rise = { id: 103, shop_id: 1, competitor_id: 8, kind: "price_increase", severity: "low", title: "Roggebol up", summary: "", competitor: "Broodhuis", competitor_domain: "broodhuis.nl", product: "Roggebol", previous_price: 2.4, new_price: 2.75, currency: "EUR", delta: 0.35, delta_pct: 14.6, evidence_url: null, detected_at: "2026-11-10T08:00:00Z", acknowledged_at: "2026-11-10T12:00:00Z", vs_you: null };

const report = { id: 5, shop_id: 1, kind: "weekly", status: "COMPLETED", title: "", period_start: "2026-11-03T00:00:00Z", period_end: "2026-11-09T23:59:59Z", facts: { shop: { name: "Bakkerij De Vries" } }, change_ids: [101, 102, 103], model: "", language: "en", error: null, created_at: "2026-11-09T23:59:59Z", completed_at: "2026-11-10T00:05:00Z" };
const overviewBody = { shops: [shop], competitor_count: 2, competitors_healthy: 1, competitors_failing: 1, changes_this_week: 3, unacknowledged: 2, recent_changes: [cut, soldOut, rise], latest_report: report };

// The fetch routes the view uses; each test installs its own handler.
const routes = (overview) => (href) => {
  if (href === "/overview") return json(overview);
  if (href.startsWith("/competitors")) return json({ count: 2, competitors: [compHtml, compFeed] });
  throw new Error(`unexpected fetch ${href}`);
};

// Poll one macrotask at a time until the view has settled; never a fixed sleep.
async function waitFor(fn, what) {
  for (let i = 0; i < 100; i++) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error(`timed out waiting for ${what}`);
}

const renderOverview = async (overview, { navigate = () => {}, extraRoutes = {} } = {}) => {
  resetEnv();
  resetDom();
  setFetch((href) => (extraRoutes[href] ? extraRoutes[href]() : routes(overview)(href)));
  const host = document.createElement("div");
  ov.default(host, { navigate });
  await waitFor(() => host.querySelector(".row-open") || host.querySelector(".first-run") || host.querySelector(".error-state"), "the overview to settle");
  return host;
};

test("isoWeek follows ISO 8601 week numbering", () => {
  assert.equal(isoWeek(new Date(2026, 10, 14)), 46); // Saturday of the week that starts Monday 9 Nov
  assert.equal(isoWeek(new Date(2025, 11, 29)), 1); // Monday 29 Dec belongs to week 1 of 2026
  assert.equal(isoWeek(new Date(2026, 0, 4)), 1); // Sunday 4 Jan is still in week 1
  assert.equal(isoWeek(new Date(2026, 0, 5)), 2); // Monday 5 Jan starts week 2
});

test("weekLabel names the ISO week with its Monday and Sunday", () => {
  const sat = new Date(2026, 10, 14);
  assert.equal(weekLabel(sat), `${t("week.label", { n: 46 })} · ${fmtDate(new Date(2026, 10, 9))} → ${fmtDate(new Date(2026, 10, 15))}`);
});

test("the first paint is a loading board with empty tiles", async () => {
  resetEnv();
  resetDom();
  setFetch(routes(overviewBody));
  const host = document.createElement("div");
  ov.default(host, { navigate: () => {} }); // run() fills the loading board synchronously before its first await

  const el = host.querySelector(".board.is-loading");
  assert.ok(el);
  assert.equal(el.getAttribute("aria-busy"), "true");
  assert.equal(el.getAttribute("aria-label"), t("ui.loading"));
  assert.equal(el.querySelector(".board-title").textContent, t("ui.loading"));
  assert.equal(el.querySelectorAll(".board-row").length, 5); // default n is five
  assert.ok(el.querySelector(".board-row").classList.contains("mark-row-exact")); // boardRow's default mark

  await waitFor(() => host.querySelector(".row-open"), "the board to settle");
  assert.equal(host.querySelector(".is-loading"), null); // the loading frame is gone once data lands
});

test("boardLegend lists only the marks the board shows", () => {
  const source = new Map([[7, "html"], [8, "api"]]);
  // cut: html competitor (extracted), unacknowledged; soldOut: api competitor (exact) and struck
  const legend = boardLegend([cut, soldOut], source);
  assert.equal(legend.tagName, "UL");
  assert.ok(legend.classList.contains("board-legend"));
  assert.equal(legend.getAttribute("aria-label"), t("legend.label"));
  const items = [...legend.children];
  assert.equal(items.length, 4); // exact, extracted, struck and unread all appear
  assert.equal(items[0].textContent, t("legend.exact"));
  assert.equal(items[1].textContent, t("legend.extracted"));
  assert.equal(items[2].querySelector(".legend-struck").textContent, "ABC");
  assert.ok(items[2].textContent.endsWith(t("legend.struck"))); // the sample letters precede the label
  assert.equal(items[3].textContent, t("legend.unread"));

  const quiet = boardLegend([rise], source); // acknowledged api competitor: exact only
  assert.deepEqual([...quiet.children].map((li) => li.textContent), [t("legend.exact")]);
  assert.equal(boardLegend([], new Map()).children.length, 0); // nothing to explain
});

test("changeRow links the row to its product history and keeps evidence separate", () => {
  const opened = [];
  const li = changeRow(cut, "html", () => opened.push(1));
  assert.equal(li.tagName, "LI");
  assert.ok(li.classList.contains("board-row"));
  assert.ok(li.classList.contains("is-link"));
  assert.ok(li.classList.contains("mark-row-extracted")); // html source is the hatched mark
  assert.equal(li.getAttribute("data-unread"), ""); // unacknowledged lamp

  const open = li.children[0];
  assert.equal(open.tagName, "A");
  assert.ok(open.classList.contains("row-open"));
  assert.equal(open.getAttribute("href"), "#/product?change=101");
  open.click();
  assert.deepEqual(opened, [1]);

  const evidence = li.children.at(-1);
  assert.ok(evidence.classList.contains("row-evidence"));
  assert.equal(evidence.getAttribute("href"), cut.evidence_url);
  assert.equal(evidence.getAttribute("target"), "_blank");
  assert.equal(evidence.getAttribute("rel"), "noopener noreferrer");

  const plain = changeRow(rise, "api", null); // acknowledged, no evidence link
  assert.ok(plain.classList.contains("mark-row-exact"));
  assert.equal(plain.getAttribute("data-unread"), null);
  assert.ok(plain.children.at(-1).classList.contains("board-cells")); // cells stay last without a link
});

test("the week's changes render as board rows with counts, legend and the report teaser", async () => {
  const nav = [];
  const host = await renderOverview(overviewBody, { navigate: (to) => nav.push(to) });

  assert.equal(host.querySelector(".view-title").textContent, t("ov.title"));
  assert.equal(host.querySelector(".view-sub").textContent, weekLabel()); // the same call the view makes
  assert.equal(host.querySelector(".is-loading"), null); // settled past the loading board

  const cols = [...host.querySelector(".board-cols").children].map((s) => s.textContent);
  assert.deepEqual(cols, [t("ov.col.competitor"), t("ov.col.product"), t("ov.col.was"), t("ov.col.now"), t("ov.col.change"), ""]);

  const rows = host.querySelectorAll(".board-row");
  assert.equal(rows.length, 3);

  // Row one: an unacknowledged price cut from the html competitor.
  const r0 = rows[0];
  assert.ok(r0.classList.contains("mark-row-extracted"));
  assert.equal(r0.getAttribute("data-tone"), "down");
  assert.equal(r0.getAttribute("data-unread"), "");
  const was = fmtMoney(4.5, "EUR");
  const now = fmtMoney(3.95, "EUR");
  assert.deepEqual([...r0.querySelectorAll(".strip")].map((s) => s.textContent), ["Bakkerij Jansen", "Zwarte brood"]);
  assert.deepEqual([...r0.querySelectorAll(".fig")].map((f) => f.textContent), [was, now, "−12%"]); // was, now, rounded pct
  assert.equal(r0.querySelector(".row-say").textContent, `${t("ch.unread")} ${t("ch.say.down", { who: "Bakkerij Jansen", what: "Zwarte brood", was, now, pct: "−12%" })} (${t("src.extracted")})`); // the spoken sentence says how sure it is

  // Row two: sold out reads as struck with the SOLD OUT strip.
  const r1 = rows[1];
  assert.ok(r1.classList.contains("is-struck"));
  assert.equal(r1.getAttribute("data-tone"), "stock");
  assert.deepEqual([...r1.querySelectorAll(".strip")].map((s) => s.textContent), ["Broodhuis", "Vlaams brood", t("ch.soldout")]);
  assert.deepEqual([...r1.querySelectorAll(".fig")].map((f) => f.textContent), [fmtMoney(5.2, "EUR")]); // only the current price
  assert.equal(r1.querySelector(".row-say").textContent, `${t("ch.unread")} ${t("ch.say.soldout", { who: "Broodhuis", what: "Vlaams brood" })}`);

  // Row three: an acknowledged rise has no unread lamp.
  const r2 = rows[2];
  assert.equal(r2.getAttribute("data-unread"), null);
  assert.deepEqual([...r2.querySelectorAll(".fig")].map((f) => f.textContent), [fmtMoney(2.4, "EUR"), fmtMoney(2.75, "EUR"), "+15%"]);

  // The row link goes to the product history; clicking it navigates without a hash jump.
  const open = r0.querySelector(".row-open");
  assert.equal(open.getAttribute("href"), "#/product?change=101");
  open.click();
  assert.deepEqual(nav, ["product?change=101"]);

  // Counts: changes this week, unread, competitors watched, failing (warn tone).
  const counts = host.querySelectorAll(".board-count");
  assert.equal(counts.length, 4);
  assert.equal(counts[0].querySelector(".flaps").getAttribute("aria-label"), "3");
  assert.equal(counts[1].querySelector(".flaps").getAttribute("aria-label"), "2");
  assert.equal(counts[2].querySelector(".flaps").getAttribute("aria-label"), "2");
  assert.equal(counts[3].getAttribute("data-tone"), "warn");
  assert.equal(counts[3].querySelector(".flaps").getAttribute("aria-label"), "1");
  assert.equal(counts[3].children[1].textContent, t("ov.count.failing.one")); // singular for one

  // Board head and the report teaser.
  assert.equal(host.querySelector("#week-board-title").textContent, t("ov.board.title"));
  const all = host.querySelector(".board-link");
  assert.equal(all.getAttribute("href"), "#/alerts");
  assert.equal(all.textContent, t("ov.board.all"));

  const teaser = host.querySelector(".report-teaser");
  assert.equal(teaser.getAttribute("href"), "#/reports");
  assert.equal(teaser.children[0].textContent, t("ov.report.latest"));
  assert.equal(teaser.children[1].textContent, `${t("rp.title", { shop: "Bakkerij De Vries" })} · ${fmtDate(new Date(report.period_end), { day: "numeric", month: "short" })}`);
  assert.equal(teaser.children[2].textContent, t("ov.report.read"));

  // The legend explains every mark on the board.
  const legend = host.querySelector(".board-legend");
  assert.equal(legend.children.length, 4);
});

test("an account with no shops sees the start prompt instead of a board", async () => {
  const empty = { shops: [], competitor_count: 0, competitors_healthy: 0, competitors_failing: 0, changes_this_week: 0, unacknowledged: 0, recent_changes: [], latest_report: null };
  const host = await renderOverview(empty);

  assert.equal(host.querySelector(".board"), null); // no board frame at all
  const fr = host.querySelector(".first-run");
  assert.ok(fr);
  assert.equal(fr.querySelector("h1.view-title").textContent, t("ov.first.title"));
  assert.equal(fr.querySelector(".view-sub").textContent, t("ov.first.body"));
  const cta = fr.querySelector("a.btn");
  assert.equal(cta.getAttribute("href"), "#/start");
  assert.equal(cta.getAttribute("data-variant"), "primary");
  assert.equal(cta.getAttribute("data-size"), "lg");
  assert.equal(cta.textContent, t("ov.first.cta"));
});

test("an API failure shows the error state and retry reloads", async () => {
  resetEnv();
  resetDom();
  let failing = true;
  setFetch((href) => (failing && href === "/overview" ? json({ detail: "boom" }, 500) : routes(overviewBody)(href)));
  const host = document.createElement("div");
  ov.default(host, { navigate: () => {} });
  await waitFor(() => host.querySelector(".error-state"), "the error state");

  const err = host.querySelector(".error-state");
  assert.equal(err.getAttribute("role"), "alert");
  assert.equal(err.querySelector("h3").textContent, t("ov.error"));
  assert.equal(err.querySelector("p").textContent, "boom"); // the API's detail string, verbatim
  const retry = err.querySelector("button.btn");
  assert.ok(retry);

  failing = false;
  retry.click();
  await waitFor(() => host.querySelector(".row-open"), "the board after retry");
  assert.equal(host.querySelector(".error-state"), null);
  assert.equal(host.querySelectorAll(".board-row").length, 3);
});

test("a quiet week still shows the last moves", async () => {
  const quietBody = { ...overviewBody, changes_this_week: 0, unacknowledged: 1, recent_changes: [] };
  const older = [cut, { id: 201, shop_id: 1, competitor_id: 8, kind: "new_product", severity: "low", title: "", summary: "", competitor: "Broodhuis", competitor_domain: "broodhuis.nl", product: "Havervlaai", previous_price: null, new_price: 3.6, currency: "EUR", delta: null, delta_pct: null, evidence_url: null, detected_at: "2026-10-28T08:00:00Z", acknowledged_at: "2026-10-29T09:00:00Z", vs_you: null }];
  let changesHit = false;
  const host = await renderOverview(quietBody, {
    extraRoutes: { "/changes?days=60&limit=8": () => { changesHit = true; return json({ count: 2, unacknowledged: 1, changes: older }); } },
  });

  assert.ok(changesHit); // the view asked for earlier moves when the week was quiet
  const quiet = host.querySelector(".board-quiet");
  assert.equal(quiet.children[0].textContent, t("ov.quiet.title"));
  const long = { weekday: "long", day: "numeric", month: "long" }; // the same options quietWeek uses
  const last = ["2026-11-08T09:30:00Z", "2026-11-08T09:35:00Z"].sort().at(-1); // newest competitor crawl
  assert.equal(quiet.textContent, `${t("ov.quiet.title")} ${t("ov.quiet.steady", { when: fmtDate(new Date(last), long) })}`);

  assert.equal(host.querySelector(".board-sub").textContent, t("ov.earlier"));
  const rows = host.querySelectorAll(".board-row");
  assert.equal(rows.length, 2); // only the earlier moves, no main board
  assert.ok(rows[0].classList.contains("mark-row-extracted")); // cut is from the html competitor

  const legend = host.querySelector(".board-legend");
  assert.deepEqual([...legend.children].map((li) => li.textContent), [t("legend.exact"), t("legend.extracted"), t("legend.unread")]); // no struck this time
});
