// alerts.js on the DOM fake: changes grouped by ISO week, the unread and kind filters
// changing the request they fire, mark-all asking before it acks (and reporting partial
// failures), and the empty and error states. Fixtures mirror the API shapes built in
// app/api/commerce.py (_shop_dict, _competitor_dict, _change_dict).
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import alertsView from "../../app/web/views/alerts.js";
import { t } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom, Event } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// The shop and its two competitors: one html-sourced (the hatched mark), one api-sourced (exact).
const shop = { id: 1, name: "Bakkerij De Vries", url: "https://devries.nl", domain: "devries.nl", platform: "shopify", category: "bakery", currency: "EUR", crawl_frequency_hours: 24, is_active: true, competitor_count: 2, last_crawled_at: "2026-11-08T09:30:00Z", next_crawl_at: "2026-11-15T09:30:00Z", last_status: "COMPLETED", created_at: "2026-09-01T10:00:00Z" };
const compHtml = { id: 7, shop_id: 1, domain: "bakkerijjansen.nl", name: "Bakkerij Jansen", url: "https://bakkerijjansen.nl", platform: "woocommerce", discovery_method: "search", confidence: 0.92, is_active: true, product_count: 48, last_crawled_at: "2026-11-08T09:30:00Z", last_status: "COMPLETED", data_source: "html", source_label: "Extracted from the page", created_at: "2026-09-02T10:00:00Z" };
const compFeed = { id: 8, shop_id: 1, domain: "broodhuis.nl", name: "Broodhuis", url: "https://broodhuis.nl", platform: "shopify", discovery_method: "search", confidence: 0.97, is_active: true, product_count: 64, last_crawled_at: "2026-11-08T09:35:00Z", last_status: "FAILED", data_source: "api", source_label: "Exact", created_at: "2026-09-02T10:05:00Z" };

// Four changes across two ISO weeks (today is the Saturday of week 46): two in week 46,
// two in week 45. Newest first, as /changes orders them; three are still unread.
const cut = { id: 301, shop_id: 1, competitor_id: 7, kind: "price_decrease", severity: "high", title: "Zwarte brood down again", summary: "", competitor: "Bakkerij Jansen", competitor_domain: "bakkerijjansen.nl", product: "Zwarte brood", previous_price: 4.5, new_price: 3.95, currency: "EUR", delta: -0.55, delta_pct: -12.2, evidence_url: "https://bakkerijjansen.nl/zwarte-brood", detected_at: "2026-11-13T08:00:00Z", acknowledged_at: null, vs_you: null };
const soldOut = { id: 302, shop_id: 1, competitor_id: 8, kind: "out_of_stock", severity: "medium", title: "Vlaams brood gone", summary: "", competitor: "Broodhuis", competitor_domain: "broodhuis.nl", product: "Vlaams brood", previous_price: null, new_price: 5.2, currency: "EUR", delta: null, delta_pct: null, evidence_url: null, detected_at: "2026-11-12T08:00:00Z", acknowledged_at: null, vs_you: null };
const rise = { id: 303, shop_id: 1, competitor_id: 8, kind: "price_increase", severity: "low", title: "Roggebol up", summary: "", competitor: "Broodhuis", competitor_domain: "broodhuis.nl", product: "Roggebol", previous_price: 2.4, new_price: 2.75, currency: "EUR", delta: 0.35, delta_pct: 14.6, evidence_url: null, detected_at: "2026-11-07T08:00:00Z", acknowledged_at: "2026-11-08T12:00:00Z", vs_you: null };
const newProd = { id: 304, shop_id: 1, competitor_id: 7, kind: "new_product", severity: "low", title: "New: Haring", summary: "", competitor: "Bakkerij Jansen", competitor_domain: "bakkerijjansen.nl", product: "Haring", previous_price: null, new_price: 3.5, currency: "EUR", delta: null, delta_pct: null, evidence_url: null, detected_at: "2026-11-03T08:00:00Z", acknowledged_at: null, vs_you: null };

const changesBody = { count: 4, unacknowledged: 3, changes: [cut, soldOut, rise, newProd] };

// Render the alerts view against a scripted /changes route and record what it asked for.
function renderAlerts({ changes = () => json(changesBody), shops = [shop], ack = null } = {}) {
  resetEnv();
  resetDom();
  resetToasts(); // ui.js caches its toasts host; a fresh document needs a fresh one.
  const host = document.createElement("main");
  document.body.appendChild(host);
  const changesUrls = [];
  const acks = [];
  setFetch((href) => {
    if (href.startsWith("/changes?")) { changesUrls.push(href); return Promise.resolve(changes()); }
    if (href.endsWith("/ack")) { acks.push(href); return ack ? Promise.resolve(ack(href)) : Promise.resolve(new Response(null, { status: 204 })); }
    if (href === "/competitors") return json({ count: 2, competitors: [compHtml, compFeed] });
    if (href === "/shops") return json({ shops });
    throw new Error(`unexpected fetch ${href}`);
  });
  alertsView(host, { navigate: () => {} });
  return { host, changesUrls, acks };
}

// Poll one macrotask at a time until the view has settled; never a fixed sleep.
async function waitFor(fn, what) {
  for (let i = 0; i < 100; i++) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error(`timed out waiting for ${what}`);
}

const settled = (host) => host.querySelectorAll(".week-board").length === 2 && !host.querySelector(".is-loading");
const markAllButton = (host) => [...host.querySelectorAll("button")].find((b) => b.textContent === t("al.markAll", { n: 3 }));

test("alerts groups the last 90 days by ISO week, newest week first", async () => {
  const { host } = renderAlerts();
  await waitFor(() => settled(host), "the alerts to settle");

  assert.equal(host.querySelector(".view-title").textContent, t("nav.alerts"));
  assert.equal(host.querySelector(".view-sub").textContent, t("al.sub"));

  const [w46, w45] = host.querySelectorAll(".week-board");
  assert.equal(w46.querySelector(".board-title").textContent, t("week.label", { n: 46 }));
  assert.equal(w45.querySelector(".board-title").textContent, t("week.label", { n: 45 }));

  // API order (newest first) is kept inside a week.
  const rows = [...host.querySelectorAll(".board-row")];
  assert.deepEqual(rows.map((r) => r.querySelector("a.row-open").getAttribute("href")), [
    "#/product?change=301", "#/product?change=302", "#/product?change=303", "#/product?change=304"]);

  // The unread lamp marks the unacknowledged rows only.
  assert.equal(rows[0].dataset.unread, "");
  assert.equal(rows[1].dataset.unread, "");
  assert.equal(rows[2].dataset.unread, undefined); // rise was acknowledged
  assert.equal(rows[3].dataset.unread, "");

  // The toolbar offers the filters and a mark-all for the three unread changes.
  assert.ok(markAllButton(host), "mark-all button shows the unread count");
});

test("the unread and kind filters change the /changes request", async () => {
  const { host, changesUrls } = renderAlerts();
  await waitFor(() => settled(host), "the alerts to settle");
  assert.deepEqual(changesUrls, ["/changes?days=90&limit=200"]);

  // Unread only: flipping the checkbox re-requests with unacknowledged_only.
  const cb = host.querySelector("#al-unread");
  cb.checked = true;
  cb.dispatchEvent(new Event("change"));
  // The request fires while the loading board is up, so wait for the toolbar to come back too.
  await waitFor(() => changesUrls.at(-1) === "/changes?days=90&limit=200&unacknowledged_only=true" && host.querySelector("#al-kind"), "the filtered request and re-render");

  // A kind on top of unread: both params ride along.
  const sel = host.querySelector("#al-kind");
  sel.value = "price_decrease";
  sel.dispatchEvent(new Event("change"));
  await waitFor(() => changesUrls.at(-1) === "/changes?days=90&limit=200&kind=price_decrease&unacknowledged_only=true", "the kind-filtered request");
});

test("mark-all asks before acking anything, and cancel backs out", async () => {
  const { host, acks } = renderAlerts();
  await waitFor(() => settled(host), "the alerts to settle");

  markAllButton(host).click();
  await waitFor(() => document.querySelector("dialog"), "the confirm dialog");

  const dlg = document.querySelector("dialog");
  assert.equal(dlg.querySelector("#dlg-title").textContent, t("al.markAllTitle", { n: 3 }));
  assert.equal(dlg.querySelector(".dialog-body").querySelector("p").textContent, t("al.markAllBody"));
  // Asked first: nothing is acked while the dialog is open.
  assert.deepEqual(acks, []);

  const cancel = dlg.querySelector("button[data-variant=ghost]");
  assert.equal(cancel.textContent, t("ui.cancel"));
  cancel.click();
  await waitFor(() => document.querySelector("dialog") === null, "the dialog to close");
  assert.deepEqual(acks, []); // still nothing acked after a back-out
  assert.equal(document.querySelector(".toast"), null);
});

test("confirming mark-all acks every unread change and toasts", async () => {
  const { host, acks } = renderAlerts();
  await waitFor(() => settled(host), "the alerts to settle");

  markAllButton(host).click();
  await waitFor(() => document.querySelector("dialog"), "the confirm dialog");
  const confirm = document.querySelector("dialog").querySelector("button[data-variant=primary]");
  assert.equal(confirm.textContent, t("al.markAllConfirm"));
  confirm.click();

  // The already-read change (303) is not acked again.
  await waitFor(() => acks.length === 3 && document.querySelector(".toast"), "the acks and the toast");
  assert.deepEqual(acks, ["/changes/301/ack", "/changes/302/ack", "/changes/304/ack"]);

  const toast = document.querySelector(".toast");
  assert.equal(toast.dataset.tone, "success");
  assert.equal(toast.querySelector("div").textContent, t("al.marked"));

  await waitFor(() => document.querySelector("dialog") === null, "the dialog to close");
  await waitFor(() => settled(host), "the view to re-run after marking");
});

test("a failed ack still reports how many were marked", async () => {
  const { host, acks } = renderAlerts({
    ack: (href) => href === "/changes/302/ack" ? json({ detail: "boom" }, 500) : new Response(null, { status: 204 }),
  });
  await waitFor(() => settled(host), "the alerts to settle");

  markAllButton(host).click();
  await waitFor(() => document.querySelector("dialog"), "the confirm dialog");
  document.querySelector("dialog").querySelector("button[data-variant=primary]").click();

  await waitFor(() => acks.length === 3 && document.querySelector(".toast"), "the acks and the toast");
  const toast = document.querySelector(".toast");
  assert.equal(toast.dataset.tone, "danger");
  assert.equal(toast.querySelector("div").textContent, t("al.markedSome", { done: 2, n: 3 }));
});

test("an account with a shop but no changes gets the plain empty plate", async () => {
  const { host } = renderAlerts({ changes: () => json({ count: 0, unacknowledged: 0, changes: [] }) });
  await waitFor(() => host.querySelector(".empty-plate"), "the empty state");

  assert.equal(host.querySelector(".week-board"), null);
  const plate = host.querySelector(".empty-plate");
  assert.equal(plate.querySelector("h2").textContent, t("al.empty.title"));
  assert.equal(plate.querySelector("p").textContent, t("al.empty.body"));
  // Nothing to mark: no mark-all button; and a shop exists: no start CTA.
  const toolbar = host.querySelector(".al-toolbar");
  assert.equal(toolbar.querySelectorAll("button").length, 0);
  assert.equal(plate.querySelector("a.btn"), null);
});

test("an account without a shop gets the start CTA instead", async () => {
  const { host } = renderAlerts({ changes: () => json({ count: 0, unacknowledged: 0, changes: [] }), shops: [] });
  await waitFor(() => host.querySelector(".empty-plate"), "the empty state");

  const plate = host.querySelector(".empty-plate");
  assert.equal(plate.querySelector("h2").textContent, t("al.empty.noShop"));
  const cta = plate.querySelector("a.btn");
  assert.equal(cta.getAttribute("href"), "#/start");
  assert.equal(cta.textContent, t("ov.first.cta"));
});

test("a failed /changes request shows the error state and retry works", async () => {
  let failing = true;
  const { host } = renderAlerts({ changes: () => (failing ? json({ detail: "boom" }, 500) : json(changesBody)) });
  await waitFor(() => host.querySelector(".error-state"), "the error state");

  const err = host.querySelector(".error-state");
  assert.equal(err.querySelector("h3").textContent, t("al.error"));
  assert.equal(err.querySelector("p").textContent, "boom");

  failing = false;
  err.querySelector(".btn").click(); // retry re-runs the same load
  await waitFor(() => settled(host), "the alerts after retry");
});

