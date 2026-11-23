// intelligence.js on the DOM fake: starting a research job (the POST /intelligence/jobs body),
// the jobs list with its pipeline stages and statuses, the crawled stores table, the opportunities
// list with its kind filter tabs, one opportunity's detail dialog, and the empty and error states.
// Fixtures mirror the response shapes in app/api/intelligence.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import intelligenceView from "../../app/web/views/intelligence.js";
import { t } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom, Event, setHidden } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// Fixtures mirror the shapes intelligence.py builds.
const jobQueued = { id: 11, status: "QUEUED", stage: null, stats: {}, error: null };
const jobRunning = { id: 10, status: "RUNNING", stage: "crawl", stats: { domains_discovered: 4, pages_crawled: 37 }, error: null };
const jobDone = { id: 9, status: "COMPLETED", stage: "scoring", stats: { domains_discovered: 2, pages_crawled: 18, products_discovered: 64, opportunities_found: 5 }, error: null };
const jobsBody = { count: 3, jobs: [jobQueued, jobRunning, jobDone] };

const storeA = { id: 1, domain: "ceramica.example", name: "Ceramica Studio", niche: "homeware", platform: "Shopify", crawl_status: "COMPLETED", product_count: 64 };
const storeB = { id: 2, domain: "plainobjects.example", name: null, niche: null, platform: null, crawl_status: "FAILED", product_count: 0 };
const storesBody = { count: 2, stores: [storeA, storeB] };

// Deliberately not in score order so the table's sort is what puts them right.
const oppPrice = { id: 102, type: "pricing", title: "Vases priced above the market median", summary: null, score: 64.1, confidence: 0.55, competition_level: "high", status: "OPEN", source_urls: [] };
const oppGap = { id: 101, type: "product_gap", title: "Matte stoneware mugs are missing from two top shops", summary: "High search volume, thin supply.", score: 82.4, confidence: 0.91, competition_level: "low", status: "OPEN", source_urls: ["https://a.example/mugs"] };
const oppsBody = { count: 2, opportunities: [oppPrice, oppGap] };

// The detail endpoint returns more than the list rows do.
const oppDetail = {
  id: 101,
  type: "product_gap",
  title: oppGap.title,
  summary: oppGap.summary,
  recommended_action: "List three matte stoneware mugs at the market median.",
  score: 82.4,
  confidence: 0.91,
  evidence: { search_volume: 1250, supply_shops: 2, price_range_usd: [18, 34] },
  source_urls: ["https://a.example/mugs", "https://b.example/vases"],
  status: "OPEN",
};

// Render the intelligence view; record every call as [method, href] and every POST body.
// jobsByCall: (n) => body | [body, status] for the /intelligence/jobs?limit=15 answers, counted from 1 —
// mount loads the panel and checks for running jobs (calls 1-2), then each poll tick adds more.
function renderIntelligence({ jobs = jobsBody, stores = storesBody, opps = oppsBody, failJobs = false, failOpps = false, failStores = false, jobsFailuresLeft = 0, startResponse = null, detailResponse = null, jobsByCall = null } = {}) {
  resetEnv();
  resetDom();
  resetToasts(); // ui.js caches its toasts host; a fresh document needs a fresh one.
  intervals.clear(); // drop any poll interval a previous test left running (the fake registry is module-scoped)
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  const posts = [];
  let limit15Calls = 0; // how many times jobsByCall has answered /intelligence/jobs?limit=15
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (method === "POST" && href === "/intelligence/jobs") {
      posts.push(JSON.parse(init.body));
      return startResponse ?? json({ job_id: 12, status: "QUEUED", created: true }, 202);
    }
    if (href.startsWith("/intelligence/jobs?")) {
      if (jobsFailuresLeft > 0 && href === "/intelligence/jobs?limit=15") {
        jobsFailuresLeft -= 1; // the first limit=15 call is the jobs panel, not the poll check
        return json({ detail: "Research jobs unavailable" }, 503);
      }
      if (failJobs) return json({ detail: "Research jobs unavailable" }, 503);
      if (jobsByCall && href === "/intelligence/jobs?limit=15") {
        const answer = jobsByCall(++limit15Calls);
        return Array.isArray(answer) ? json(answer[0], answer[1]) : json(answer);
      }
      return json(jobs);
    }
    if (href.startsWith("/intelligence/stores?")) {
      if (failStores) return json({ detail: "Store crawl index down" }, 502);
      return json(stores);
    }
    if (href.startsWith("/intelligence/opportunities?")) {
      const kind = new URLSearchParams(href.split("?")[1]).get("kind");
      if (failOpps) return json({ detail: "Opportunity model down" }, 503);
      if (kind) {
        const filtered = opps.opportunities.filter((o) => o.type === kind); // mirrors the API's type filter
        return json({ count: filtered.length, opportunities: filtered });
      }
      return json(opps);
    }
    if (/^\/intelligence\/opportunities\/\d+$/.test(href)) {
      return detailResponse ?? json(oppDetail);
    }
    throw new Error(`unexpected fetch ${method} ${href}`);
  });
  // The view polls every 5s while a job is queued or running; the returned cleanup clears that interval.
  const stop = intelligenceView(host);
  return { host, calls, posts, stop };
}

// Poll one macrotask at a time until the predicate holds; never a fixed sleep.
async function waitFor(fn, what) {
  const deadline = Date.now() + 5000;
  for (;;) {
    if (fn()) return;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((resolve) => setImmediate(resolve));
  }
}

// Every async panel has left its skeleton state (data or error rendered).
const settled = (host) => host.querySelector("[aria-busy]") === null;
const sectionWith = (host, title) => [...host.querySelectorAll("section")].find((s) => s.querySelector("h2")?.textContent === title);

// intelligence.js polls on a real 5s setInterval; swap in a fake we fire by hand so no test
// waits real time and no interval can outlive its test (a live one would keep Node from exiting).
const intervals = new Map();
let nextIntervalId = 0;
Object.defineProperty(globalThis, "setInterval", {
  value: (fn) => { const id = ++nextIntervalId; intervals.set(id, fn); return id; },
  writable: true, configurable: true,
});
Object.defineProperty(globalThis, "clearInterval", {
  value: (id) => { if (id != null) intervals.delete(id); },
  writable: true, configurable: true,
});

// Fire every live interval once and await the work each callback starts.
async function firePolls() {
  const started = [...intervals.values()].map((fn) => fn());
  await Promise.allSettled(started);
}

test("mounting loads stats, jobs, opportunities and stores from their endpoints", async () => {
  const { host, calls, stop } = renderIntelligence();
  try {
    await waitFor(() => settled(host), "all panels to settle");

    assert.equal(host.querySelector("h1").textContent, "Intelligence");
    assert.equal(host.querySelector(".page-head p").textContent, "Crawl competitor stores, extract products and score market opportunities.");
    const gets = [...new Set(calls.filter(([m]) => m === "GET").map(([, h]) => h))].sort();
    assert.deepEqual(gets, [
      "/intelligence/jobs?limit=100", // stats panel
      "/intelligence/jobs?limit=15", // jobs panel plus the poll check
      "/intelligence/opportunities?limit=200",
      "/intelligence/opportunities?limit=60",
      "/intelligence/stores?limit=200",
      "/intelligence/stores?limit=40",
    ]);
    assert.deepEqual([...host.querySelectorAll("section.card h2")].map((h) => h.textContent), ["New research job", "Research jobs", "Opportunities", "Crawled stores"]);
  } finally {
    stop();
  }
});

test("starting research posts the query and seed URLs, then toasts the new job id", async () => {
  const { host, calls, posts, stop } = renderIntelligence();
  try {
    const form = host.querySelector("form");
    const query = form.querySelector('input[aria-label="Research query"]');
    const urls = form.querySelector('input[aria-label="Seed URLs"]');
    assert.equal(query.getAttribute("minlength"), "3"); // the client enforces the API's minimum
    query.value = "minimalist ceramic homeware";
    urls.value = "https://a.example, https://b.example ,";

    form.dispatchEvent(new Event("submit"));
    await waitFor(() => posts.length === 1, "the POST to land");
    assert.deepEqual(posts[0], { query: "minimalist ceramic homeware", start_urls: ["https://a.example", "https://b.example"] }); // trimmed and split on commas
    assert.ok(calls.some(([m, h]) => m === "POST" && h === "/intelligence/jobs"), "the job is queued through POST /intelligence/jobs");

    await waitFor(() => document.querySelector('.toast[data-tone="success"]'), "the success toast");
    assert.equal(document.querySelector('.toast[data-tone="success"]').querySelector("div").textContent, "Research job #12 queued");
    assert.equal(query.value, ""); // the form clears on success
    assert.equal(urls.value, "");
  } finally {
    stop();
  }
});

test("a query under three characters is refused without any request", async () => {
  const { host, calls, posts, stop } = renderIntelligence();
  try {
    const form = host.querySelector("form");
    form.querySelector('input[aria-label="Research query"]').value = "ab";
    form.dispatchEvent(new Event("submit"));

    await waitFor(() => document.querySelector('.toast[data-tone="danger"]'), "the danger toast");
    assert.match(document.querySelector('.toast[data-tone="danger"]').textContent, /at least 3 characters/);
    assert.equal(posts.length, 0); // nothing left the browser
    assert.equal(calls.filter(([m]) => m === "POST").length, 0);
  } finally {
    stop();
  }
});

test("a failed start toasts the API error and keeps the form filled", async () => {
  const { host, posts, stop } = renderIntelligence({ startResponse: json({ detail: "Crawler is busy" }, 503) });
  try {
    const form = host.querySelector("form");
    const query = form.querySelector('input[aria-label="Research query"]');
    query.value = "minimalist ceramic homeware";

    form.dispatchEvent(new Event("submit"));
    await waitFor(() => posts.length === 1, "the POST to land");
    await waitFor(() => document.querySelector('.toast[data-tone="danger"]'), "the danger toast");
    assert.equal(document.querySelector('.toast[data-tone="danger"]').querySelector("div").textContent, "Crawler is busy"); // api.js lifts detail into the message
    assert.equal(query.value, "minimalist ceramic homeware"); // not cleared on failure
  } finally {
    stop();
  }
});

test("the jobs list shows stage, stats and a status badge per job", async () => {
  const { host, stop } = renderIntelligence();
  try {
    await waitFor(() => settled(host), "the jobs panel to settle");

    const section = sectionWith(host, "Research jobs");
    assert.equal(section.querySelector(".sub").textContent, "Pipeline progress per job");
    const rows = [...section.querySelectorAll(".row")];
    assert.equal(rows.length, 3); // newest first, as the API returns them

    // Stage names are title-cased into the row title; a null stage leaves it bare.
    assert.deepEqual(rows.map((r) => r.querySelector(".row-title").textContent), ["Job #11", "Job #10 · Crawl", "Job #9 · Scoring"]);
    // Stats render as "N domains · N pages · ..."; empty stats fall back to the error or a waiting note.
    assert.deepEqual(rows.map((r) => r.querySelector(".row-sub").textContent), ["waiting to start", "4 domains · 37 pages", "2 domains · 18 pages · 64 products · 5 opportunities"]);

    const badges = rows.map((r) => r.querySelector(".row-side .badge"));
    assert.deepEqual(badges.map((b) => b.textContent), ["QUEUED", "RUNNING", "COMPLETED"]); // title() uppercases the first letter only; all-caps statuses stay as-is
    assert.deepEqual(badges.map((b) => b.getAttribute("data-tone")), ["warning", "info", "success"]);
    // In-flight jobs carry a live dot; finished ones do not.
    assert.ok(rows[0].querySelector(".row-side .badge .dot"), "queued job is marked live");
    assert.ok(rows[1].querySelector(".row-side .badge .dot"), "running job is marked live");
    assert.equal(rows[2].querySelector(".row-side .badge .dot"), null);

    // The meter tracks the pipeline stage: queued 5%, crawl half way, done full and green.
    const meters = rows.map((r) => r.querySelector(".meter"));
    assert.deepEqual(meters.map((m) => m.querySelector("span").style.width), ["5%", "50%", "100%"]);
    assert.equal(meters[2].getAttribute("data-tone"), "success");
    assert.equal(meters[0].getAttribute("data-tone"), null); // in-flight meters stay neutral
  } finally {
    stop();
  }
});

test("a failed job shows its error text and a red meter", async () => {
  const failed = { id: 8, status: "FAILED", stage: "crawl", stats: {}, error: "Crawler timed out" };
  const { host, stop } = renderIntelligence({ jobs: { count: 1, jobs: [failed] } });
  try {
    await waitFor(() => settled(host), "the jobs panel to settle");

    const row = sectionWith(host, "Research jobs").querySelector(".row");
    assert.equal(row.querySelector(".row-title").textContent, "Job #8 · Crawl");
    assert.equal(row.querySelector(".row-sub").textContent, "Crawler timed out"); // the error replaces the empty stats line
    const badge = row.querySelector(".row-side .badge");
    assert.equal(badge.textContent, "FAILED"); // title() does not lowercase all-caps statuses
    assert.equal(badge.getAttribute("data-tone"), "danger");
    assert.equal(row.querySelector(".meter").getAttribute("data-tone"), "danger");
  } finally {
    stop();
  }
});

test("no research jobs yet shows the empty plate", async () => {
  const { host, stop } = renderIntelligence({ jobs: { count: 0, jobs: [] } });
  try {
    await waitFor(() => settled(host), "the jobs panel to settle");

    const section = sectionWith(host, "Research jobs");
    assert.equal(section.querySelector(".row"), null);
    const plate = section.querySelector(".empty");
    assert.equal(plate.querySelector("h3").textContent, "No research yet");
    assert.match(plate.querySelector("p").textContent, /Start a job above/);
  } finally {
    stop();
  }
});

test("the stores table shows domain, niche, platform, product count and crawl status", async () => {
  const { host, stop } = renderIntelligence();
  try {
    await waitFor(() => settled(host), "the stores panel to settle");

    const section = sectionWith(host, "Crawled stores");
    assert.equal(section.querySelector(".sub").textContent, "External sources feeding the opportunity model");
    const rows = [...section.querySelectorAll("tbody tr")];
    assert.equal(rows.length, 2);

    // A named store shows its name with the domain as a sub-line.
    const cellsA = rows[0].querySelectorAll("td");
    assert.equal(cellsA[0].querySelector(".truncate").textContent, "Ceramica Studio");
    assert.equal(cellsA[0].querySelector(".truncate").getAttribute("title"), "ceramica.example"); // full domain on hover
    assert.equal(cellsA[0].querySelector(".row-sub.mono").textContent, "ceramica.example");
    const niche = cellsA[1].querySelector("span.badge");
    assert.equal(niche.textContent, "Homeware");
    assert.equal(niche.getAttribute("data-tone"), "neutral");
    assert.equal(cellsA[2].textContent, "Shopify");
    assert.equal(cellsA[3].textContent, "64");
    const statusA = cellsA[4].querySelector("span.badge");
    assert.equal(statusA.textContent, "COMPLETED");
    assert.equal(statusA.getAttribute("data-tone"), "success");

    // A store without name, niche or platform falls back to the domain and dashes.
    const cellsB = rows[1].querySelectorAll("td");
    assert.equal(cellsB[0].querySelector(".truncate").textContent, "plainobjects.example");
    assert.equal(cellsB[0].querySelector(".row-sub"), null); // no sub-line without a name
    assert.equal(cellsB[1].querySelector("span.badge"), null);
    assert.equal(cellsB[1].textContent, "\u2014");
    assert.equal(cellsB[2].textContent, "\u2014");
    assert.equal(cellsB[3].textContent, "0");
    const statusB = cellsB[4].querySelector("span.badge");
    assert.equal(statusB.textContent, "FAILED");
    assert.equal(statusB.getAttribute("data-tone"), "danger");
  } finally {
    stop();
  }
});

test("no crawled stores yet shows the empty plate", async () => {
  const { host, stop } = renderIntelligence({ stores: { count: 0, stores: [] } });
  try {
    await waitFor(() => settled(host), "the stores panel to settle");

    const section = sectionWith(host, "Crawled stores");
    assert.equal(section.querySelector("tbody tr"), null);
    const plate = section.querySelector(".empty");
    assert.equal(plate.querySelector("h3").textContent, "No stores crawled");
    assert.match(plate.querySelector("p").textContent, /discovered during research/);
  } finally {
    stop();
  }
});

test("the opportunities table sorts by score and shows type, competition, confidence and status", async () => {
  const { host, calls, stop } = renderIntelligence();
  try {
    await waitFor(() => settled(host), "the opportunities panel to settle");

    assert.ok(calls.some(([m, h]) => m === "GET" && h === "/intelligence/opportunities?limit=60"), "the list loads without a kind filter");
    const section = sectionWith(host, "Opportunities");
    const rows = [...section.querySelectorAll("tbody tr")];
    assert.equal(rows.length, 2);

    // The fixture is not in score order; the table sorts descending.
    assert.deepEqual(rows.map((r) => r.querySelector(".truncate").textContent), [oppGap.title, oppPrice.title]);
    assert.equal(rows[0].querySelector(".row-sub").textContent, oppGap.summary);
    assert.equal(rows[1].querySelector(".row-sub").textContent, ""); // null summary renders empty

    const typeBadges = rows.map((r) => r.querySelectorAll("td")[1].querySelector("span.badge"));
    assert.deepEqual(typeBadges.map((b) => b.textContent), ["Product Gap", "Pricing"]);
    assert.ok(typeBadges.every((b) => b.getAttribute("data-tone") === "primary"));

    const comp = rows.map((r) => r.querySelectorAll("td")[2].querySelector("span.badge"));
    assert.deepEqual(comp.map((b) => b.textContent), ["Low", "High"]);
    assert.equal(comp[0].getAttribute("data-tone"), "success");
    assert.equal(comp[1].getAttribute("data-tone"), "danger");

    // Confidence is a percentage of the 0..1 score; the raw score keeps one decimal.
    // The fake DOM ignores :nth-child/:last-child, so pick cells by index (0-based).
    assert.deepEqual(rows.map((r) => r.querySelectorAll("td")[3].textContent), ["91%", "55%"]);
    assert.deepEqual(rows.map((r) => r.querySelectorAll("td")[4].textContent), ["82.4", "64.1"]);

    const status = rows[0].querySelectorAll("td")[5].querySelector("span.badge");
    assert.equal(status.textContent, "OPEN"); // title() leaves all-caps statuses untouched
    assert.equal(status.getAttribute("data-tone"), "neutral");
  } finally {
    stop();
  }
});

test("clicking a kind tab sends the filtered request and re-renders the table", async () => {
  const { host, calls, stop } = renderIntelligence();
  try {
    await waitFor(() => settled(host), "the opportunities panel to settle");

    const section = sectionWith(host, "Opportunities");
    const tabs = [...section.querySelectorAll(".tabs button.tab")];
    assert.deepEqual(tabs.map((b) => b.textContent), ["All", "Product gaps", "Pricing", "Niches"]);
    assert.equal(tabs[0].getAttribute("aria-selected"), "true"); // All is selected by default

    tabs[2].click(); // Pricing
    await waitFor(() => calls.some(([m, h]) => m === "GET" && h === "/intelligence/opportunities?limit=60&kind=pricing"), "the filtered request");
    assert.equal(tabs[2].getAttribute("aria-selected"), "true");
    assert.equal(tabs[0].getAttribute("aria-selected"), "false");

    await waitFor(() => settled(host), "the opportunities panel to re-settle");
    const rows = [...section.querySelectorAll("tbody tr")];
    assert.equal(rows.length, 1); // only the pricing opportunity survives the filter
    assert.equal(rows[0].querySelector(".truncate").textContent, oppPrice.title);
  } finally {
    stop();
  }
});

test("no opportunities in a view shows the empty plate", async () => {
  const { host, stop } = renderIntelligence({ opps: { count: 0, opportunities: [] } });
  try {
    await waitFor(() => settled(host), "the opportunities panel to settle");

    const section = sectionWith(host, "Opportunities");
    assert.equal(section.querySelector("tbody tr"), null);
    const plate = section.querySelector(".empty");
    assert.equal(plate.querySelector("h3").textContent, "No opportunities in this view");
    assert.match(plate.querySelector("p").textContent, /type filter/);
  } finally {
    stop();
  }
});

test("opening an opportunity shows its evidence, action and sources in a dialog", async () => {
  const { host, calls, stop } = renderIntelligence();
  try {
    await waitFor(() => settled(host), "the opportunities panel to settle");

    sectionWith(host, "Opportunities").querySelector("tbody tr").click(); // first row is oppGap (id 101)
    await waitFor(() => calls.some(([m, h]) => m === "GET" && h === "/intelligence/opportunities/101"), "the detail request");
    const dlg = document.querySelector("dialog");
    assert.ok(dlg, "a dialog opens for the clicked opportunity");
    await waitFor(() => dlg.querySelector(".dialog-body h3"), "the opportunity detail to render"); // the fetch resolves after the request is recorded

    assert.equal(dlg.querySelector(".dialog-body h3").textContent, oppDetail.title);
    const chips = [...dlg.querySelectorAll(".chips .badge")];
    assert.deepEqual(chips.map((b) => b.textContent), ["Product Gap", "Score 82.4", "91% Confidence", "OPEN"]); // badge() title-cases its text; OPEN stays all caps
    assert.equal(dlg.querySelector(".dialog-body p").textContent, oppDetail.summary);

    // The recommended action is offered as an info banner.
    const banner = dlg.querySelector('.banner[data-tone="info"]');
    assert.ok(banner, "a recommended-action banner is shown");
    assert.equal(banner.querySelector("div").textContent, oppDetail.recommended_action);

    // Evidence renders as a definition list; objects are stringified, numbers stay plain.
    const dts = [...dlg.querySelectorAll(".kv dt")].map((d) => d.textContent);
    assert.deepEqual(dts, ["Search Volume", "Supply Shops", "Price Range Usd"]);
    const dds = [...dlg.querySelectorAll(".kv dd")].map((d) => d.textContent);
    assert.deepEqual(dds, ["1250", "2", "[18,34]"]);

    // Every source URL becomes a link with its full text in the title attribute.
    const links = [...dlg.querySelectorAll("a.truncate")];
    assert.deepEqual(links.map((a) => a.getAttribute("href")), oppDetail.source_urls);
    assert.equal(links[0].getAttribute("title"), oppDetail.source_urls[0]);

    // The Close button removes the dialog from the document.
    const close = [...dlg.querySelectorAll(".dialog-foot button")].find((b) => b.textContent === t("ui.close"));
    assert.ok(close, "a close button is offered");
    close.click();
    await waitFor(() => document.querySelector("dialog") === null, "the dialog to close");
  } finally {
    stop();
  }
});

test("a missing opportunity shows the error state inside the dialog", async () => {
  const { host, stop } = renderIntelligence({ detailResponse: json({ detail: "Opportunity not found" }, 404) });
  try {
    await waitFor(() => settled(host), "the opportunities panel to settle");

    sectionWith(host, "Opportunities").querySelector("tbody tr").click();
    const dlg = document.querySelector("dialog");
    assert.ok(dlg, "the dialog still opens while the request is in flight");
    await waitFor(() => dlg.querySelector(".error-state"), "the error state to render");

    const err = dlg.querySelector(".error-state");
    assert.equal(err.getAttribute("role"), "alert");
    assert.equal(err.querySelector("h3").textContent, t("ui.error"));
    assert.equal(err.querySelector("p").textContent, "Opportunity not found"); // api.js lifts detail into the message
  } finally {
    stop();
  }
});

test("a failed jobs load shows an error state whose retry recovers", async () => {
  const { host, stop } = renderIntelligence({ jobsFailuresLeft: 1 });
  try {
    await waitFor(() => settled(host), "the jobs panel to settle");

    const section = sectionWith(host, "Research jobs");
    const err = section.querySelector(".error-state");
    assert.ok(err, "the jobs panel reports the failure instead of a skeleton");
    assert.equal(err.getAttribute("role"), "alert");
    assert.equal(err.querySelector("h3").textContent, t("ui.error"));
    assert.equal(err.querySelector("p").textContent, "Research jobs unavailable");

    const retry = [...err.querySelectorAll("button")].find((b) => b.textContent === t("ui.retry"));
    assert.ok(retry, "a retry button is offered");
    retry.click();
    await waitFor(() => section.querySelectorAll(".row").length === 3, "the jobs to reload after retry");
    assert.equal(section.querySelector(".error-state"), null);
  } finally {
    stop();
  }
});

test("when every stats source fails the metrics panel shows an error state", async () => {
  const { host, stop } = renderIntelligence({ failJobs: true, failOpps: true, failStores: true });
  try {
    await waitFor(() => settled(host), "all panels to settle");

    // The stats host is the second child of the view (after the page head).
    const statsHost = host.children[1];
    const err = statsHost.querySelector(".error-state");
    assert.ok(err, "the metrics panel reports the failure");
    assert.equal(err.getAttribute("role"), "alert");
    assert.equal(err.querySelector("p").textContent, "Research jobs unavailable"); // first rejected source wins

    // Each data panel degrades on its own with its own message.
    assert.equal(sectionWith(host, "Crawled stores").querySelector(".error-state p").textContent, "Store crawl index down");
    assert.equal(sectionWith(host, "Opportunities").querySelector(".error-state p").textContent, "Opportunity model down");
  } finally {
    stop();
  }
});

/* ------------------------------------------------- polling for running jobs */

test("the poll refreshes the panels and stops once every job has finished", async () => {
  const completedJobsBody = { count: 1, jobs: [jobDone] };
  // Calls 1-2 are mount (panel load + running check); from call 3 on nothing is running.
  const { host, calls, stop } = renderIntelligence({
    jobsByCall: (n) => (n <= 2 ? jobsBody : completedJobsBody),
  });
  try {
    await waitFor(() => settled(host), "the view to settle");

    const count = (href) => calls.filter(([m, h]) => m === "GET" && h === href).length;
    assert.equal(count("/intelligence/jobs?limit=15"), 2); // panel load + mount-time check

    await firePolls();
    await waitFor(() => settled(host), "the panels to settle after the poll");

    const rows = [...sectionWith(host, "Research jobs").querySelectorAll(".row")];
    assert.deepEqual(rows.map((r) => r.querySelector(".row-title").textContent), ["Job #9 · Scoring"]); // reloaded from the finished answer
    assert.equal(count("/intelligence/jobs?limit=15"), 4); // the poll's own check + reloadJobs()
    assert.equal(count("/intelligence/opportunities?limit=60"), 2); // mount + reloadOpps()
    assert.equal(count("/intelligence/jobs?limit=100"), 2); // the stats panel reloaded too

    await firePolls(); // the interval was cleared, so nothing happens now
    assert.equal(count("/intelligence/jobs?limit=15"), 4);
  } finally {
    stop();
  }
});

test("a failed poll clears the interval and leaves the panels as they were", async () => {
  const { host, calls, stop } = renderIntelligence({
    jobsByCall: (n) => (n <= 2 ? jobsBody : [{ detail: "Research jobs unavailable" }, 503]),
  });
  try {
    await waitFor(() => settled(host), "the view to settle");

    const count15 = () => calls.filter(([m, h]) => m === "GET" && h === "/intelligence/jobs?limit=15").length;
    assert.equal(count15(), 2); // panel load + mount-time check

    await firePolls(); // the tick's own check fails -> catch clears the interval
    assert.equal(count15(), 3); // only the failed check: no reloadJobs after it
    const rows = [...sectionWith(host, "Research jobs").querySelectorAll(".row")];
    assert.equal(rows.length, 3); // stale rows stay; this panel shows no error state

    await firePolls();
    assert.equal(count15(), 3); // nothing is scheduled anymore
  } finally {
    stop();
  }
});
test("a poll answer without a jobs key stops polling and empties the list", async () => {
  const { host, calls, stop } = renderIntelligence({
    jobsByCall: (n) => (n <= 2 ? jobsBody : {}), // no .jobs anywhere in the answer
  });
  try {
    await waitFor(() => settled(host), "the view to settle");

    const count15 = () => calls.filter(([m, h]) => m === "GET" && h === "/intelligence/jobs?limit=15").length;
    assert.equal(count15(), 2); // panel load + mount-time check

    await firePolls();
    await waitFor(() => settled(host), "the panels to settle after the poll");

    const section = sectionWith(host, "Research jobs");
    assert.equal(section.querySelector(".row"), null);
    assert.equal(section.querySelector(".empty h3").textContent, "No research yet"); // reloadJobs({}) renders the empty plate
    assert.equal(count15(), 4); // the poll's check + reloadJobs()

    await firePolls();
    assert.equal(count15(), 4); // nothing is scheduled anymore
  } finally {
    stop();
  }
});

test("the poll skips its tick while the tab is hidden", async () => {
  const { host, calls, stop } = renderIntelligence(); // default fixture has running jobs -> polling starts
  try {
    await waitFor(() => settled(host), "the view to settle");

    const count15 = () => calls.filter(([m, h]) => m === "GET" && h === "/intelligence/jobs?limit=15").length;
    assert.equal(count15(), 2); // panel load + mount-time check (which started the poll)

    setHidden(true);
    await firePolls(); // the callback returns before fetching
    assert.equal(count15(), 2);

    setHidden(false);
    await firePolls(); // now it polls again and reloads the jobs panel
    assert.equal(count15(), 4); // the tick's check + reloadJobs()
  } finally {
    stop();
  }
});

/* ------------------------------------------------- sorting opportunities */

// Score and confidence disagree on the order so each sortable header sorts differently.
const sortFixtures = {
  count: 2,
  opportunities: [
    { id: 201, type: "pricing", title: "High score low confidence", summary: null, score: 90.0, confidence: 0.4, competition_level: "medium", status: "OPEN" },
    { id: 202, type: "niche", title: "Low score high confidence", summary: null, score: 50.0, confidence: 0.95, status: "OPEN" }, // no competition level
  ],
};

test("clicking a sortable header sorts by it and flips the direction on repeat clicks", async () => {
  const { host, stop } = renderIntelligence({ opps: sortFixtures });
  try {
    await waitFor(() => settled(host), "the view to settle");

    const section = sectionWith(host, "Opportunities");
    const titles = () => [...section.querySelectorAll("tbody tr")].map((tr) => tr.children[0].textContent.trim());
    assert.deepEqual(titles(), ["High score low confidence", "Low score high confidence"]); // default: score descending

    const ths = [...section.querySelectorAll("thead th")];
    const [titleTh, , , confTh, scoreTh] = ths;
    assert.equal(scoreTh.getAttribute("aria-sort"), "descending"); // the initial sort owns Score

    scoreTh.click(); // same column: flip to ascending
    assert.deepEqual(titles(), ["Low score high confidence", "High score low confidence"]);
    assert.equal(scoreTh.getAttribute("aria-sort"), "ascending");
    assert.ok(ths.filter((th) => th.hasAttribute("data-sortable") && th !== scoreTh).every((th) => th.getAttribute("aria-sort") === "none"));

    confTh.click(); // a different column: sort by it, descending again
    assert.deepEqual(titles(), ["Low score high confidence", "High score low confidence"]); // 0.95 > 0.4
    assert.equal(confTh.getAttribute("aria-sort"), "descending");
    assert.equal(scoreTh.getAttribute("aria-sort"), "none");

    confTh.click(); // flip it back
    assert.deepEqual(titles(), ["High score low confidence", "Low score high confidence"]);
    assert.equal(confTh.getAttribute("aria-sort"), "ascending");

    titleTh.click(); // the string column sorts with localeCompare, descending
    assert.deepEqual(titles(), ["Low score high confidence", "High score low confidence"]); // L > H
    assert.equal(titleTh.getAttribute("aria-sort"), "descending");
  } finally {
    stop();
  }
});

test("keyboard: Enter and space toggle a header sort, other keys do nothing", async () => {
  const { host, stop } = renderIntelligence({ opps: sortFixtures });
  try {
    await waitFor(() => settled(host), "the view to settle");

    const section = sectionWith(host, "Opportunities");
    const titles = () => [...section.querySelectorAll("tbody tr")].map((tr) => tr.children[0].textContent.trim());
    const scoreTh = [...section.querySelectorAll("thead th")][4];
    const keydown = (el, key) => { const e = new Event("keydown"); e.key = key; el.dispatchEvent(e); };

    keydown(scoreTh, "a"); // an unrelated key does nothing
    assert.deepEqual(titles(), ["High score low confidence", "Low score high confidence"]);
    assert.equal(scoreTh.getAttribute("aria-sort"), "descending");

    keydown(scoreTh, " "); // space toggles like a click
    assert.equal(scoreTh.getAttribute("aria-sort"), "ascending");
    assert.deepEqual(titles(), ["Low score high confidence", "High score low confidence"]);

    keydown(scoreTh, "Enter"); // Enter flips it back to descending
    assert.equal(scoreTh.getAttribute("aria-sort"), "descending");
  } finally {
    stop();
  }
});

test("missing competition level, confidence and score render as unknown or zero", async () => {
  const { host, stop } = renderIntelligence({
    opps: { count: 1, opportunities: [{ id: 203, type: "niche", title: "Sparse opportunity", summary: null, status: "OPEN" }] },
  });
  try {
    await waitFor(() => settled(host), "the view to settle");

    const row = sectionWith(host, "Opportunities").querySelector("tbody tr");
    assert.equal(row.children[2].textContent.trim(), "Unknown"); // no competition level -> warning badge
    assert.equal(row.children[3].textContent.trim(), "0%"); // no confidence
    assert.equal(row.children[4].textContent.trim(), "0.0"); // no score
  } finally {
    stop();
  }
});

test("a detail without summary, recommended action, evidence or sources shows only its fallbacks", async () => {
  const bare = { id: 204, type: "niche", title: "Bare opportunity", status: "OPEN" }; // no score/confidence/summary/evidence/sources
  const { host, stop } = renderIntelligence({ opps: { count: 1, opportunities: [bare] }, detailResponse: json(bare) });
  try {
    await waitFor(() => settled(host), "the view to settle");

    sectionWith(host, "Opportunities").querySelector("tbody tr").click();
    const dlg = document.querySelector("dialog");
    assert.ok(dlg, "a dialog opens for the clicked opportunity");
    await waitFor(() => dlg.querySelector(".dialog-body h3"), "the opportunity detail to render");

    assert.equal(dlg.textContent.includes("No summary."), true); // the summary fallback
    assert.equal(dlg.textContent.includes("Score 0.0"), true); // missing score -> zero chip (badge title-cases)
    assert.equal(dlg.textContent.includes("0% Confidence"), true); // missing confidence -> zero chip
    assert.equal(dlg.querySelector(".banner"), null); // no recommended-action banner
    assert.equal(dlg.querySelector("dl.kv"), null); // no evidence block
  } finally {
    stop();
  }
});










