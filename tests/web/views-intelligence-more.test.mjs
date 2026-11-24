// Branch coverage for intelligence.js's defensive fallbacks: the paths that only fire when the API answers without a list key, or when rows are missing fields:
// the mount-time polling check against a bare jobs answer, stats falling back to zero, empty plates for stores and opportunities,
// "waiting to start" on a job with no stats yet, sorting that treats a missing score as 0 (and titles as strings),
// keyboard activation of an opportunity row, and dashes in the detail dialog for missing evidence values.
// Fixtures mirror the response shapes in app/api/intelligence.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import intelligenceView from "../../app/web/views/intelligence.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom, Event } from "./dom.mjs";

const json = (body) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });

// The view polls every 5 seconds while a job is queued or running; swap in a fake so no test
// waits on real time and no interval outlives its test (a live one would keep Node from exiting).
const intervals = new Map();
Object.defineProperty(globalThis, "setInterval", { value: (fn) => { const id = Symbol("interval"); intervals.set(id, fn); return id; }, writable: true, configurable: true });
Object.defineProperty(globalThis, "clearInterval", { value: (id) => intervals.delete(id), writable: true, configurable: true });

// A completed job with full stats and a running one that has no stats key at all.
const jobDone = { id: 9, status: "COMPLETED", stage: "scoring", stats: { domains_discovered: 2, pages_crawled: 18, products_discovered: 64, opportunities_found: 5 }, error: null };
const jobNoStats = { id: 8, status: "RUNNING", stage: null, error: null }; // no stats key yet
const jobsBody = { count: 2, jobs: [jobDone, jobNoStats] };

const storeA = { id: 1, domain: "ceramica.example", name: "Ceramica Studio", niche: "homeware", platform: "Shopify", crawl_status: "COMPLETED", product_count: 64 };
const storesBody = { count: 1, stores: [storeA] };

// Deliberately not in score order; the first has no score at all.
const oppNoScore = { id: 201, type: "niche", title: "Zebra-print table runners", summary: null, confidence: 0.4, competition_level: "low", status: "OPEN" }; // no score key
const oppHigh = { id: 202, type: "pricing", title: "Pricing above the median", summary: null, score: 81.5, confidence: 0.7, competition_level: "high", status: "OPEN" };
const oppLow = { id: 203, type: "product_gap", title: "Gap in matte mugs", summary: null, score: 42.2, confidence: 0.6, competition_level: "low", status: "OPEN" };
const oppsBody = { count: 3, opportunities: [oppNoScore, oppHigh, oppLow] };

// The detail endpoint returns more than the list rows do.
const oppDetail = { id: 202, type: "pricing", title: oppHigh.title, summary: null, recommended_action: "Reprice the vase line to the market median.", score: 81.5, confidence: 0.7, competition_level: "high", status: "OPEN", source_urls: ["https://a.example/vases"], evidence: { search_volume: 1200, supply_shops: 9 } };
// A duck-typed Response for endpoints that must answer with a plain object (JSON cannot carry undefined values).
const plain = (body) => ({ ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => body });

function renderIntelligence({ jobs = jobsBody, stores = storesBody, opps = oppsBody, detailResponse = null } = {}) {
  resetEnv(); resetDom(); resetToasts(); intervals.clear();
  const host = document.createElement("main");
  document.body.appendChild(host);
  setFetch((href) => {
    if (href.startsWith("/intelligence/jobs?")) return json(jobs);
    if (href.startsWith("/intelligence/stores?")) return json(stores);
    if (href.startsWith("/intelligence/opportunities?")) return json(opps);
    if (/^\/intelligence\/opportunities\/\d+$/.test(href)) {
      // A duck-typed Response, not a real one: JSON cannot carry undefined values and one test needs them.
      return detailResponse ?? plain(oppDetail);
    }
    throw new Error(`unexpected fetch ${href}`);
  });
  intelligenceView(host);
  return host;
}

// Poll one macrotask at a time until the view has settled; never a fixed sleep.
async function waitFor(fn, what) {
  for (let i = 0; i < 200; i += 1) {
    if (fn()) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.fail(`timed out waiting for ${what}`);
}

const sectionWith = (host, title) => [...host.querySelectorAll("section.card")].find((s) => s.querySelector("h2").textContent === title);
const statCard = (host, label) => [...host.querySelectorAll(".grid-metrics .card.metric")].find((m) => m.querySelector(".metric-label")?.textContent === label);

test("a jobs answer without its list starts no polling and shows the empty plate", async () => {
  const host = renderIntelligence({ jobs: {} }); // bare object: no "jobs" key anywhere
  await waitFor(() => sectionWith(host, "Research jobs").querySelector(".empty") && statCard(host, "Research jobs"), "the jobs panel and stats to settle");
  assert.equal(sectionWith(host, "Research jobs").querySelector(".empty h3").textContent, "No research yet");
  assert.equal(intervals.size, 0); // nothing queued or running: no poll timer
  const card = statCard(host, "Research jobs");
  assert.equal(card.querySelector(".metric-value").textContent, "0");
  assert.equal(card.querySelector(".metric-sub").textContent, "all complete");
});

test("bare stores and opportunities answers show empty plates and zero stats", async () => {
  const host = renderIntelligence({ stores: {}, opps: {} }); // no "stores" / "opportunities" keys anywhere
  await waitFor(() => sectionWith(host, "Crawled stores").querySelector(".empty") && sectionWith(host, "Opportunities").querySelector(".empty"), "both empty plates");
  assert.equal(sectionWith(host, "Crawled stores").querySelector(".empty h3").textContent, "No stores crawled");
  assert.equal(sectionWith(host, "Opportunities").querySelector(".empty h3").textContent, "No opportunities in this view");
  const opps = statCard(host, "Opportunities");
  assert.equal(opps.querySelector(".metric-value").textContent, "0"); // count missing: num(undefined) is zero
  assert.equal(opps.querySelector(".metric-sub").textContent, "no low-competition finds");
  assert.equal(statCard(host, "Best score").querySelector(".metric-value").textContent, "—");
  const stores = statCard(host, "Stores crawled");
  assert.equal(stores.querySelector(".metric-value").textContent, "0");
  assert.equal(stores.querySelector(".metric-sub").textContent, "0 external products");
});

test("a job without stats renders the waiting note", async () => {
  const host = renderIntelligence(); // jobsBody: one done with stats, one running without any
  await waitFor(() => sectionWith(host, "Research jobs").querySelectorAll(".row").length === 2, "both job rows");
  const rows = [...sectionWith(host, "Research jobs").querySelectorAll(".row")];
  assert.equal(rows[0].querySelector(".row-title").textContent, "Job #9 · Scoring"); // API order kept
  assert.equal(rows[0].querySelector(".row-sub").textContent, "2 domains · 18 pages · 64 products · 5 opportunities");
  assert.equal(rows[1].querySelector(".row-title").textContent, "Job #8");
  assert.equal(rows[1].querySelector(".row-sub").textContent, "waiting to start"); // no stats yet and no error
});

test("sorting treats a missing score as zero and titles as strings", async () => {
  const host = renderIntelligence(); // oppsBody: no-score, 81.5, 42.2 in that order
  await waitFor(() => sectionWith(host, "Opportunities").querySelectorAll("tbody tr").length === 3, "all opportunity rows");
  const titles = () => [...sectionWith(host, "Opportunities").querySelectorAll("tbody tr")].map((r) => r.querySelector("div.truncate").textContent);
  assert.deepEqual(titles(), ["Pricing above the median", "Gap in matte mugs", "Zebra-print table runners"]); // missing score sorts as 0, last
  const titleTh = [...sectionWith(host, "Opportunities").querySelectorAll("th")].find((c) => c.textContent.trim().startsWith("Opportunity"));
  titleTh.click();
  assert.deepEqual(titles(), ["Zebra-print table runners", "Pricing above the median", "Gap in matte mugs"]); // a new column sorts descending by string compare
  titleTh.click();
  assert.deepEqual(titles(), ["Gap in matte mugs", "Pricing above the median", "Zebra-print table runners"]); // flipped to ascending
});

test("a non-Enter key on an opportunity row does nothing; Enter opens it", async () => {
  const host = renderIntelligence();
  await waitFor(() => sectionWith(host, "Opportunities").querySelectorAll("tbody tr").length === 3, "all opportunity rows");
  const firstRow = sectionWith(host, "Opportunities").querySelector("tbody tr"); // oppHigh after the score sort
  const keydown = (el, key) => { const e = new Event("keydown"); e.key = key; el.dispatchEvent(e); };
  keydown(firstRow, "x"); // an unrelated key does nothing
  assert.equal(document.querySelector("dialog"), null);
  keydown(firstRow, "Enter");
  await waitFor(() => document.querySelector(".dialog-body h3")?.textContent === oppHigh.title, "the detail dialog to fill");
  const dlg = document.querySelector("dialog");
  assert.equal(dlg.querySelector("#dlg-title").textContent, "Opportunity");
  assert.equal(dlg.querySelector(".dialog-body h3").textContent, oppHigh.title);
});

test("the detail dialog shows a dash for missing evidence values", async () => {
  // JSON cannot carry undefined; the fake answers this one endpoint with a plain object so the key survives.
  const sparse = { ...oppDetail, id: 201, title: oppNoScore.title, score: undefined, recommended_action: null, evidence: { search_volume: undefined, supply_shops: 3 } };
  const host = renderIntelligence({ detailResponse: plain(sparse) });
  await waitFor(() => sectionWith(host, "Opportunities").querySelectorAll("tbody tr").length === 3, "all opportunity rows");
  sectionWith(host, "Opportunities").querySelector("tbody tr").click(); // any row: the fake answers every id with `sparse`
  await waitFor(() => document.querySelector(".dialog-body h3")?.textContent === oppNoScore.title, "the detail dialog to fill");
  const dds = [...document.querySelector(".dialog-body dl.kv").querySelectorAll("dd")].map((d) => d.textContent);
  assert.deepEqual(dds, ["—", "3"]); // undefined evidence renders a dash; numbers render as-is
});
