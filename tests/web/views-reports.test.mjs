// reports.js on the DOM fake: a week list with status chips, one report read as a board of its moves
// (only that report's change ids), the written summary rendered from escaped Markdown, and "write now"
// queuing a report per shop. Fixtures mirror the API shapes built in app/api/commerce.py
// (report_to_dict, _shop_dict, _competitor_dict, _change_dict).
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import reportsView, { markdown } from "../../app/web/views/reports.js";
import { t } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { isoWeek } from "../../app/web/views/board-view.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const tick = () => new Promise((r) => setTimeout(r, 0));

// --- fixtures ---------------------------------------------------------------
// One shop with two competitors: an html-sourced one (the hatched mark) and an api-sourced one (exact).
const shopA = { id: 1, name: "Bakkerij De Vries", url: "https://devries.nl", domain: "devries.nl", platform: "shopify", category: "bakery", currency: "EUR", crawl_frequency_hours: 24, is_active: true, competitor_count: 2, last_crawled_at: "2026-11-08T09:30:00Z", next_crawl_at: "2026-11-15T09:30:00Z", last_status: "COMPLETED", created_at: "2026-09-01T10:00:00Z" };
const shopB = { id: 4, name: "Kaaswinkel Bol", url: "https://bol.nl", domain: "bol.nl", platform: "shopify", category: "cheese", currency: "EUR", crawl_frequency_hours: 24, is_active: true, competitor_count: 0, last_crawled_at: null, next_crawl_at: null, last_status: null, created_at: "2026-10-01T10:00:00Z" };
const compHtml = { id: 7, shop_id: 1, domain: "bakkerijjansen.nl", name: "Bakkerij Jansen", url: "https://bakkerijjansen.nl", platform: "woocommerce", discovery_method: "search", confidence: 0.92, is_active: true, product_count: 48, last_crawled_at: "2026-11-08T09:30:00Z", last_status: "COMPLETED", data_source: "html", source_label: "Extracted from the page", created_at: "2026-09-02T10:00:00Z" };
const compFeed = { id: 8, shop_id: 1, domain: "broodhuis.nl", name: "Broodhuis", url: "https://broodhuis.nl", platform: "shopify", discovery_method: "search", confidence: 0.97, is_active: true, product_count: 64, last_crawled_at: "2026-11-08T09:35:00Z", last_status: "COMPLETED", data_source: "api", source_label: "Exact", created_at: "2026-09-02T10:05:00Z" };

// Four changes across two weeks, newest first as /changes orders them. 304 belongs to the older
// report only — it must not appear on the newer report's board.
const cut = { id: 301, shop_id: 1, competitor_id: 7, kind: "price_decrease", severity: "high", title: "Zwarte brood down again", summary: "", competitor: "Bakkerij Jansen", competitor_domain: "bakkerijjansen.nl", product: "Zwarte brood", previous_price: 4.5, new_price: 3.95, currency: "EUR", delta: -0.55, delta_pct: -12.2, evidence_url: "https://bakkerijjansen.nl/zwarte-brood", detected_at: "2026-11-08T08:00:00Z", acknowledged_at: null, vs_you: null };
const soldOut = { id: 302, shop_id: 1, competitor_id: 8, kind: "out_of_stock", severity: "medium", title: "Vlaams brood gone", summary: "", competitor: "Broodhuis", competitor_domain: "broodhuis.nl", product: "Vlaams brood", previous_price: null, new_price: 5.2, currency: "EUR", delta: null, delta_pct: null, evidence_url: null, detected_at: "2026-11-07T08:00:00Z", acknowledged_at: "2026-11-08T12:00:00Z", vs_you: null };
const rise = { id: 303, shop_id: 1, competitor_id: 8, kind: "price_increase", severity: "low", title: "Roggebol up", summary: "", competitor: "Broodhuis", competitor_domain: "broodhuis.nl", product: "Roggebol", previous_price: 2.4, new_price: 2.75, currency: "EUR", delta: 0.35, delta_pct: 14.6, evidence_url: null, detected_at: "2026-11-05T08:00:00Z", acknowledged_at: null, vs_you: null };
const newProd = { id: 304, shop_id: 1, competitor_id: 7, kind: "new_product", severity: "low", title: "New: Haring", summary: "", competitor: "Bakkerij Jansen", competitor_domain: "bakkerijjansen.nl", product: "Haring", previous_price: null, new_price: 3.5, currency: "EUR", delta: null, delta_pct: null, evidence_url: null, detected_at: "2026-10-30T08:00:00Z", acknowledged_at: null, vs_you: null };

// The prose is our own server's Markdown; the H1 repeats the title and must be skipped.
const mdA = `# Bakkerij De Vries — week of 3 November
## The week in short
**Two cuts**, one rise, and a shelf emptied.
- Zwarte brood down again
- Vlaams brood gone
| Product | Was | Now |
| --- | --- | --- |
| Zwarte brood | €4,50 | €3,95 |
[See the page](https://bakkerijjansen.nl/zwarte-brood)
<script>alert("x")</script>`;

const reportA = { id: 12, shop_id: 1, kind: "weekly", status: "COMPLETED", title: "Weekly report — week of 3 November", period_start: "2026-11-03T00:00:00Z", period_end: "2026-11-09T23:59:59Z", facts: { shop: { name: "Bakkerij De Vries" }, total_changes: 4, by_kind: { price_decrease: 2, price_increase: 1 }, competitors: [7, 8] }, change_ids: [301, 302, 303], model: "gpt-5.2", language: "en", error: null, created_at: "2026-11-10T06:00:00Z", completed_at: "2026-11-10T06:01:00Z", markdown: mdA };
const reportB = { id: 9, shop_id: 1, kind: "weekly", status: "COMPLETED", title: "Wekelijkse rapportage — week van 27 oktober", period_start: "2026-10-27T00:00:00Z", period_end: "2026-11-02T23:59:59Z", facts: { shop: { name: "Bakkerij De Vries" }, total_changes: 1, by_kind: {}, competitors: [7] }, change_ids: [304], model: "gpt-5.2", language: "nl", error: null, created_at: "2026-11-03T06:00:00Z", completed_at: "2026-11-03T06:01:00Z", markdown: "## Korte samenvatting\nEén nieuw product deze week." };

// The routes the view hits, in terms of what it actually asks for.
function routes(state) {
  return (href, init = {}) => {
    if (href === "/reports?limit=30") return json({ count: state.reports.length, reports: state.reports });
    const one = /^\/reports\/(\d+)$/.exec(href);
    if (one) {
      const r = state.full[Number(one[1])];
      return r ? json(r) : new Response(JSON.stringify({ detail: "Report not found" }), { status: 404, headers: { "content-type": "application/json" } });
    }
    if (href === "/shops") return json({ count: state.shops.length, shops: state.shops });
    if (href === "/changes?days=365&limit=200") return json({ count: state.changes.length, changes: state.changes });
    if (href === "/competitors") return json({ count: 2, competitors: [compHtml, compFeed] });
    const gen = /^\/shops\/(\d+)\/report\?days=7$/.exec(href);
    if (gen && init.method === "POST") { state.generated.push(Number(gen[1])); return json({ message: "ok" }, 202); }
    throw new Error(`unexpected fetch ${href}`);
  };
}

const base = () => ({ shops: [shopA], reports: [reportA, reportB], full: { 12: reportA, 9: reportB }, changes: [cut, soldOut, rise, newProd], generated: [] });

/** Render the view at a hash and let its first settleAll round resolve. */
async function render(state, hash = "#/reports", navigate = () => {}) {
  resetEnv(); resetDom(); resetToasts();
  setFetch(routes(state));
  location.hash = hash;
  const host = document.createElement("main");
  reportsView(host, { navigate });
  await tick();
  return { host, navigate };
}

test("renders the week list and reads the newest report as a board", async () => {
  const state = base();
  const { host } = await render(state);

  // The list: newest first, the open one marked.
  const items = host.querySelectorAll("a.report-item");
  assert.equal(items.length, 2);
  assert.equal(items[0].getAttribute("href"), "#/reports?id=12");
  assert.equal(items[0].getAttribute("aria-current"), "page");
  assert.equal(items[1].getAttribute("href"), "#/reports?id=9");
  assert.equal(items[1].getAttribute("aria-current"), null);
  const week = t("week.label", { n: isoWeek(new Date(reportA.period_end)) });
  assert.ok(items[0].textContent.includes(week));
  assert.ok(items[1].textContent.includes(t("week.label", { n: isoWeek(new Date(reportB.period_end)) })));
  // Completed reports carry no status chip.
  assert.equal(items[0].querySelector(".report-item-status"), null);

  // The detail pane reads reportA, not the list item's own title.
  assert.equal(host.querySelector("h1.view-title").textContent, t("nav.reports"));
  assert.equal(host.querySelector("#report-title").textContent, t("rp.title", { shop: "Bakkerij De Vries" }));
  const prose = host.querySelector(".report-prose");
  assert.ok(prose);
  assert.equal(prose.querySelector("h3").textContent, t("rp.summary"));
  assert.equal(host.querySelector(".report-lang"), null); // written in the UI language

  // The board holds exactly this report's changes, newest first.
  const rows = host.querySelectorAll("li.board-row");
  assert.deepEqual(rows.map((r) => r.querySelector("a.row-open").getAttribute("href")), [
    "#/product?change=301", "#/product?change=302", "#/product?change=303"]);

  // The counts come from the report's facts, not from what is on screen.
  const counts = host.querySelector(".board-counts").children;
  assert.equal(counts.length, 4);
  assert.ok(counts[0].textContent.includes(t("ov.count.changes", { n: 4 })));
  assert.ok(counts[1].textContent.includes(t("rp.count.cuts", { n: 2 })));
  assert.ok(counts[2].textContent.includes(t("rp.count.rises.one"))); // one rise -> the .one form
  assert.ok(counts[3].textContent.includes(t("ov.count.watched", { n: 2 })));

  // The hatched mark follows the competitor's data source; acknowledged rows lose their dot.
  const [r1, r2] = rows;
  assert.ok(r1.classList.contains("mark-row-extracted"));
  assert.equal(r1.dataset.unread, "");
  assert.ok(r1.querySelector("a.row-evidence"));
  assert.ok(r2.classList.contains("mark-row-exact"));
  assert.equal(r2.hasAttribute("data-unread"), false);
  assert.equal(r2.querySelector("a.row-evidence"), null);
});

test("a board row opens the product history through navigate", async () => {
  const state = base();
  let navigated = null;
  const { host } = await render(state, "#/reports", (view) => { navigated = view; });
  host.querySelectorAll("li.board-row")[0].querySelector("a.row-open").click();
  assert.equal(navigated, "product?change=301");
});

test("selects the report from the hash id and notes its language", async () => {
  const state = base();
  const { host } = await render(state, "#/reports?id=9");

  assert.equal(host.querySelector("#report-title").textContent, t("rp.title", { shop: "Bakkerij De Vries" }));
  const rows = host.querySelectorAll("li.board-row");
  assert.deepEqual(rows.map((r) => r.querySelector("a.row-open").getAttribute("href")), ["#/product?change=304"]);

  // A report written in another language says so, and its prose keeps that language.
  assert.equal(host.querySelector(".report-lang").textContent, t("rp.writtenIn.nl"));
  const prose = host.querySelector(".report-prose");
  const langDiv = [...prose.children].find((el) => el.tagName === "DIV");
  assert.equal(langDiv.lang, "nl");
  assert.ok(host.querySelector(".prose").innerHTML.includes("<h4>Korte samenvatting</h4>"));

  // The list marks the selected week.
  const items = host.querySelectorAll("a.report-item");
  assert.equal(items[1].getAttribute("aria-current"), "page");
});

test("renders the written summary from escaped markdown", async () => {
  const state = base();
  const { host } = await render(state);
  const html = host.querySelector(".prose").innerHTML;

  // Blocks: H2 down, bold, list, table wrapped for small screens.
  assert.ok(html.includes("<h4>The week in short</h4>"));
  assert.ok(!html.includes("week of 3 November"), "the H1 repeats the title and is skipped");
  assert.ok(html.includes("<strong>Two cuts</strong>"));
  assert.ok(html.includes("<ul><li>Zwarte brood down again</li><li>Vlaams brood gone</li></ul>"));
  assert.ok(html.includes('<div class="table-wrap"><table class="readings">'));
  assert.ok(html.includes("<th>Product</th>") && html.includes("<td>€4,50</td>"));

  // Links: http(s) only, escaped first.
  assert.ok(html.includes('<a href="https://bakkerijjansen.nl/zwarte-brood" target="_blank" rel="noopener noreferrer">See the page</a>'));
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;"));
});

test("no reports yet: an empty plate that still offers writing one", async () => {
  const state = base();
  state.reports = [];
  state.full = {};
  const { host } = await render(state);

  const plate = host.querySelector("section.empty-plate");
  assert.ok(plate);
  assert.equal(plate.querySelector("h2").textContent, t("rp.empty.title"));
  assert.equal(plate.querySelector("p").textContent, t("rp.empty.body"));
  // A shop exists, so the plate offers "write now" instead of pointing to setup.
  const buttons = [...host.querySelectorAll("button")];
  assert.ok(buttons.some((b) => b.textContent === t("rp.writeNow")));
  assert.equal(host.querySelector('a[href="#/start"]'), null);
});

test("no shop yet: points to setup", async () => {
  const state = base();
  state.shops = [];
  state.reports = [];
  state.full = {};
  const { host } = await render(state);

  const plate = host.querySelector("section.empty-plate");
  assert.equal(plate.querySelector("h2").textContent, t("al.empty.noShop"));
  assert.equal(plate.querySelector("p").textContent, t("al.empty.noShopBody"));
  const cta = host.querySelector("a.btn");
  assert.equal(cta.getAttribute("href"), "#/start");
  assert.equal(cta.textContent, t("ov.first.cta"));
  assert.equal(host.querySelectorAll("button").length, 0);
});

test("an error state offers a retry", async () => {
  const state = base();
  let failing = true;
  resetEnv(); resetDom(); resetToasts();
  setFetch((href) => (failing && href === "/reports?limit=30" ? new Response(JSON.stringify({ detail: "boom" }), { status: 500, headers: { "content-type": "application/json" } }) : routes(state)(href)));
  location.hash = "#/reports";
  const host = document.createElement("main");
  reportsView(host, {});
  await tick();

  const errEl = host.querySelector(".error-state");
  assert.ok(errEl);
  assert.equal(errEl.getAttribute("role"), "alert");
  assert.equal(errEl.querySelector("h3").textContent, t("rp.error"));
  assert.equal(errEl.querySelector("p").textContent, "boom");

  failing = false;
  errEl.querySelector("button").click(); // the retry button
  await tick();
  assert.ok(host.querySelector("#report-title"));
  assert.equal(host.querySelector(".error-state"), null);
});

test("write now queues a report for every shop", async () => {
  const state = base();
  state.shops = [shopA, shopB];
  const { host } = await render(state);

  const btn = [...host.querySelectorAll("button")].find((b) => b.textContent === t("rp.writeNow"));
  assert.ok(btn);
  btn.click();
  assert.equal(btn.dataset.loading, "true"); // set before the first await suspends
  await tick();
  assert.deepEqual(state.generated, [1, 4]);
  const toast = document.querySelector(".toast");
  assert.equal(toast.dataset.tone, "success");
  assert.ok(toast.textContent.includes(t("rp.queued")));
});

test("write now reports a failure", async () => {
  const state = base();
  resetEnv(); resetDom(); resetToasts();
  setFetch((href, init) => (/^\/shops\/\d+\/report\?days=7$/.test(href) ? json({ detail: "no credits" }, 502) : routes(state)(href, init)));
  location.hash = "#/reports";
  const host = document.createElement("main");
  reportsView(host, {});
  await tick();

  const btn = [...host.querySelectorAll("button")].find((b) => b.textContent === t("rp.writeNow"));
  btn.click();
  await tick();
  assert.deepEqual(state.generated, []); // the handler refused before recording anything
  const toast = document.querySelector(".toast");
  assert.equal(toast.dataset.tone, "danger");
  assert.ok(toast.textContent.includes("no credits"));
  assert.equal(btn.hasAttribute("data-loading"), false); // cleared in finally
});

test("list items show a chip while writing or after failure", async () => {
  const state = base();
  const gen = { ...reportA, id: 20, status: "GENERATING" };
  const failed = { ...reportB, id: 21, status: "FAILED" };
  state.reports = [gen, failed];
  state.full = { 20: gen, 21: failed };
  const { host } = await render(state);

  const items = host.querySelectorAll("a.report-item");
  assert.equal(items[0].querySelector(".report-item-status").textContent, t("rp.writing"));
  assert.equal(items[1].querySelector(".report-item-status").textContent, t("rp.failed"));
});

test("markdown maps blocks and keeps only http(s) links", () => {
  const el = markdown("# Title\n## Sub\n**bold** [ok](https://x.nl) [bad](javascript:alert(1))\n- a\n| H |\n| --- |\n| v |");
  const html = el.innerHTML;
  assert.ok(!html.includes("Title"), "the H1 is skipped");
  assert.ok(html.includes("<h4>Sub</h4>"));
  assert.ok(html.includes("<strong>bold</strong>"));
  assert.ok(html.includes('<a href="https://x.nl" target="_blank" rel="noopener noreferrer">ok</a>'));
  assert.ok(html.includes("[bad](javascript:alert(1))"), "non-http links stay plain text");
  assert.ok(html.includes("<ul><li>a</li></ul>"));
  assert.ok(html.includes('<table class="readings">') && html.includes("<th>H</th>") && html.includes("<td>v</td>"));
});

test("markdown escapes everything before mapping", () => {
  const html = markdown('<script>alert(1)</script>').innerHTML;
  assert.ok(!html.includes("<script>"));
  assert.ok(html.includes("&lt;script&gt;alert(1)&lt;/script&gt;"));
});