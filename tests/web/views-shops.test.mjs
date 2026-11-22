// shops.js on the DOM fake: the shop board with competitor rows (exact / extracted / never
// crawled), check-now queuing a crawl (and surfacing the API's refusal), adding a rival —
// including plan-limit refusals shown inline instead of as toasts — and removing a rival that
// asks first. Fixtures mirror the API shapes built in app/api/commerce.py (_shop_dict,
// _competitor_dict).
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import shopsView from "../../app/web/views/shops.js";
import { t, fmtDate, fmtNumber } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom, Event } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// One shop with three rivals covering every data state — exact (feed), extracted (html),
// and one never crawled. A second shop has no rivals yet.
const shop = { id: 1, name: "Bakkerij De Vries", url: "https://devries.nl", domain: "devries.nl", platform: "shopify", category: "bakery", currency: "EUR", crawl_frequency_hours: 24, is_active: true, competitor_count: 3, last_crawled_at: "2026-11-13T09:30:00Z", next_crawl_at: "2026-11-15T09:30:00Z", last_status: "COMPLETED", created_at: "2026-09-01T10:00:00Z" };
const shopNoRivals = { id: 2, name: "Grocerij Van Dijk", url: "https://vandijk.nl", domain: "vandijk.nl", platform: "woocommerce", category: "grocery", currency: "EUR", crawl_frequency_hours: 48, is_active: true, competitor_count: 0, last_crawled_at: null, next_crawl_at: null, last_status: null, created_at: "2026-10-05T10:00:00Z" };
const compFeed = { id: 7, shop_id: 1, domain: "broodhuis.nl", name: "Broodhuis", url: "https://broodhuis.nl", platform: "shopify", discovery_method: "search", confidence: 0.97, is_active: true, product_count: 64, last_crawled_at: "2026-10-01T09:30:00Z", last_status: "COMPLETED", data_source: "feed", source_label: "Exact", created_at: "2026-09-02T10:05:00Z" };
const compHtml = { id: 8, shop_id: 1, domain: "bakkerijjansen.nl", name: "Bakkerij Jansen", url: "https://bakkerijjansen.nl", platform: "woocommerce", discovery_method: "search", confidence: 0.92, is_active: true, product_count: 48, last_crawled_at: "2026-10-01T09:35:00Z", last_status: "COMPLETED", data_source: "html", source_label: "Extracted from the page", created_at: "2026-09-02T10:00:00Z" };
const compNew = { id: 9, shop_id: 1, domain: "deeg.nl", name: "Deeg", url: "https://deeg.nl", platform: "unknown", discovery_method: "manual", confidence: null, is_active: true, product_count: 0, last_crawled_at: null, last_status: null, data_source: null, source_label: null, created_at: "2026-11-14T08:00:00Z" };
const competitorsBody = { count: 3, competitors: [compFeed, compHtml, compNew] };

// Render the shops view against scripted /shops and /competitors routes; record every call.
// `failShopsOnce` makes the first /shops load throw so the error state (and its retry) can be tested.
function renderShops({ crawl = null, createCompetitor = null, shopsBody = { shops: [shop, shopNoRivals] }, failShopsOnce = false } = {}) {
  resetEnv();
  resetDom();
  resetToasts(); // ui.js caches its toasts host; a fresh document needs a fresh one.
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  const created = [];
  let shopsFailsLeft = failShopsOnce ? 1 : 0;
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (method === "GET" && href === "/shops") {
      if (shopsFailsLeft > 0) { shopsFailsLeft -= 1; throw new Error("api down"); }
      return json(shopsBody);
    }
    if (method === "GET" && href === "/competitors") return json(competitorsBody);
    if (method === "POST" && /^\/shops\/\d+\/crawl$/.test(href)) {
      return crawl ? Promise.resolve(crawl()) : json({ job_id: "job-1", status: "queued" }, 202);
    }
    if (method === "POST" && href === "/competitors") {
      created.push(JSON.parse(init.body));
      return createCompetitor
        ? Promise.resolve(createCompetitor())
        : json({ ...compNew, id: 10, domain: "bakkerijjansen.nl", name: "Bakkerij Jansen" }, 201);
    }
    if (method === "DELETE" && /^\/competitors\/\d+$/.test(href)) return json({ deleted: true });
    throw new Error(`unexpected fetch ${method} ${href}`);
  });
  shopsView(host);
  return { host, calls, created };
}

// Poll one macrotask at a time until the view has settled; never a fixed sleep.
async function waitFor(fn, what) {
  for (let i = 0; i < 100; i++) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error(`timed out waiting for ${what}`);
}

const settled = (host) => host.querySelectorAll(".shop-block").length === 2 && !host.querySelector(".is-loading");
const shopBlock = (host, id) => [...host.querySelectorAll(".shop-block")].find((b) => b.querySelector(`#shop-${id}`));
const checkButton = (block) => [...block.querySelectorAll("button")].find((b) => b.textContent === t("sh.checkNow"));

test("the shop board lists each shop with its rivals and their data state", async () => {
  const { host } = renderShops();
  await waitFor(() => settled(host), "the shops to settle");

  assert.equal(host.querySelector(".view-title").textContent, t("sh.title"));
  assert.equal(host.querySelector(".view-sub").textContent, t("sh.sub"));
  // The header offers adding a shop once at least one exists.
  const head = host.querySelector(".view-head");
  const cta = [...head.querySelectorAll("a.btn")].find((a) => a.getAttribute("href") === "#/start"); // the fake takes compound selectors only
  assert.equal(cta.getAttribute("href"), "#/start");
  assert.equal(cta.textContent, t("sh.addShop"));

  const first = shopBlock(host, 1);
  assert.equal(first.querySelector("#shop-1").textContent, shop.name);
  // Meta line: the domain link, the cadence in words, and the next crawl date.
  const meta = first.querySelector(".shop-meta");
  assert.equal(meta.querySelector("a").getAttribute("href"), shop.url);
  assert.ok(meta.textContent.includes(t("plan.freq.daily")));
  assert.ok(meta.textContent.includes(t("sh.next", { when: fmtDate(new Date(shop.next_crawl_at), { weekday: "short", day: "numeric", month: "short" }) })));

  // The three rivals keep API order and carry their data state.
  const rows = [...first.querySelectorAll(".comp-row")];
  assert.deepEqual(rows.map((r) => r.dataset.state), ["exact", "extracted", "new"]);
  assert.equal(rows[0].querySelector(".src-tag").textContent, t("src.exact"));
  assert.equal(rows[1].querySelector(".src-tag").textContent, t("src.extracted"));
  assert.equal(rows[2].querySelector(".src-tag").getAttribute("data-src"), "none");
  assert.ok(rows[0].querySelector(".comp-count").textContent.startsWith(t("sh.products", { n: fmtNumber(64) })));
  assert.equal(rows[2].querySelector(".comp-count").textContent, t("sh.waitingFirst"));
});

test("a shop without rivals shows the empty plate and a disabled Check now", async () => {
  const { host } = renderShops();
  await waitFor(() => settled(host), "the shops to settle");
  const second = shopBlock(host, 2);
  assert.equal(second.querySelector("#shop-2").textContent, shopNoRivals.name);
  assert.equal(second.querySelector(".comp-empty").textContent, t("sh.noRivals"));
  assert.equal(second.querySelector(".comp-list"), null);
  assert.ok(checkButton(second).disabled, "Check now is disabled without rivals");
});

test("check now queues a crawl, toasts and reloads", async () => {
  const { host, calls } = renderShops();
  await waitFor(() => settled(host), "the shops to settle");
  checkButton(shopBlock(host, 1)).click();
  await waitFor(() => document.querySelector(".toast") && calls.length === 5, "the queued toast and the reload");
  assert.deepEqual(calls, [
    ["GET", "/shops"], ["GET", "/competitors"],
    ["POST", "/shops/1/crawl"],
    ["GET", "/shops"], ["GET", "/competitors"],
  ]);
  const toast = document.querySelector(".toast");
  assert.equal(toast.dataset.tone, "success");
  assert.equal(toast.querySelector("div").textContent, t("sh.checkQueued"));
});

test("a refused crawl shows the API's error and does not reload", async () => {
  const { host, calls } = renderShops({ crawl: () => json({ detail: "Add at least one competitor before crawling." }, 400) });
  await waitFor(() => settled(host), "the shops to settle");
  checkButton(shopBlock(host, 1)).click();
  await waitFor(() => document.querySelector(".toast"), "the error toast");
  assert.equal(calls.length, 3); // no reload after a refusal
  const toast = document.querySelector(".toast");
  assert.equal(toast.dataset.tone, "danger");
  assert.equal(toast.querySelector("div").textContent, "Add at least one competitor before crawling.");
});

test("adding a rival posts the url and shop, toasts and reloads", async () => {
  const { host, calls, created } = renderShops();
  await waitFor(() => settled(host), "the shops to settle");
  const form = shopBlock(host, 1).querySelector("form.comp-add");
  form.querySelector("#add-rival-1").value = "bakkerijjansen.nl"; // no scheme: the view prefixes https://
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => document.querySelector(".toast") && calls.length === 5, "the added toast and the reload");
  assert.deepEqual(created, [{ url: "https://bakkerijjansen.nl", shop_id: 1 }]);
  assert.deepEqual(calls, [
    ["GET", "/shops"], ["GET", "/competitors"],
    ["POST", "/competitors"],
    ["GET", "/shops"], ["GET", "/competitors"],
  ]);
  const toast = document.querySelector(".toast");
  assert.equal(toast.dataset.tone, "success");
  assert.equal(toast.querySelector("div").textContent, t("sh.added"));
});

test("a plan limit shows the upgrade hint inline instead of a toast", async () => {
  const { host, calls } = renderShops({ createCompetitor: () => json({ detail: { error: "plan_limit_reached", message: "Your Free plan includes 3 competitors.", upgrade: true } }, 402) });
  await waitFor(() => settled(host), "the shops to settle");
  const form = shopBlock(host, 1).querySelector("form.comp-add");
  const msg = form.querySelector(".form-error");
  form.querySelector("#add-rival-1").value = "https://deeg.nl"; // with scheme: passed through as-is
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => msg.hidden === false && msg.textContent !== "", "the inline refusal");
  assert.equal(msg.textContent, `Your Free plan includes 3 competitors. ${t("sh.upgrade")}`);
  assert.equal(calls.length, 3); // no reload after a refusal
  assert.equal(document.querySelector(".toast"), null);
});

test("removing a rival asks first; canceling backs out", async () => {
  const { host, calls } = renderShops();
  await waitFor(() => settled(host), "the shops to settle");
  const row = shopBlock(host, 1).querySelectorAll(".comp-row")[1]; // Bakkerij Jansen (id 8)
  [...row.querySelectorAll("button")].find((b) => b.textContent === t("sh.remove")).click();
  await waitFor(() => document.querySelector("dialog"), "the confirm dialog");

  const dlg = document.querySelector("dialog");
  assert.equal(dlg.querySelector("#dlg-title").textContent, t("sh.removeTitle", { name: compHtml.name }));
  assert.equal(dlg.querySelector(".dialog-body").querySelector("p").textContent, t("sh.removeBody"));
  // Asked first: nothing is deleted while the dialog is open.
  assert.deepEqual(calls.filter(([m]) => m === "DELETE"), []);

  const cancel = dlg.querySelector("button[data-variant=ghost]");
  assert.equal(cancel.textContent, t("ui.cancel"));
  cancel.click();
  await waitFor(() => document.querySelector("dialog") === null, "the dialog to close");
  assert.deepEqual(calls.filter(([m]) => m === "DELETE"), []); // still nothing deleted after a back-out
  assert.equal(document.querySelector(".toast"), null);
});

test("confirming the removal deletes the rival and toasts", async () => {
  const { host, calls } = renderShops();
  await waitFor(() => settled(host), "the shops to settle");
  const row = shopBlock(host, 1).querySelectorAll(".comp-row")[0]; // Broodhuis (id 7)
  [...row.querySelectorAll("button")].find((b) => b.textContent === t("sh.remove")).click();
  await waitFor(() => document.querySelector("dialog"), "the confirm dialog");
  const confirm = document.querySelector("dialog").querySelector("button[data-variant=danger]");
  assert.equal(confirm.textContent, t("sh.remove"));
  confirm.click();

  await waitFor(() => document.querySelector(".toast") && calls.length === 5, "the removed toast and the reload");
  assert.deepEqual(calls, [
    ["GET", "/shops"], ["GET", "/competitors"],
    ["DELETE", "/competitors/7"],
    ["GET", "/shops"], ["GET", "/competitors"],
  ]);
  const toast = document.querySelector(".toast");
  assert.equal(toast.dataset.tone, "success");
  assert.equal(toast.querySelector("div").textContent, t("sh.removed"));
});
