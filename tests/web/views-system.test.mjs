// system.js on the DOM fake: the four health tiles from /health, how a failing dependency is
// reported (warning banner plus red tiles), the agent tool registry grouped by category with its
// access and approval badges, the Prometheus exposition parsed into name/value rows, the empty
// counters plate and each panel's error state. Fixtures mirror GET /health, GET /tools and
// GET /metrics from app/api/health.py (tool entries as Tool.to_info() in app/agent/registry.py).
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import systemView from "../../app/web/views/system.js";
import { t } from "../../app/web/i18n.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
// /metrics is served as text/plain; version=0.0.4, so api.metrics() returns the raw exposition.
const prometheus = (body, status = 200) => new Response(body, { status, headers: { "content-type": "text/plain; version=0.0.4" } });

// The shapes exactly as health.py builds them.
const healthOk = {
  status: "ok",
  database: { ok: true, url_scheme: "sqlite" },
  llm: { provider: "openai", available: true, model: "gpt-4o-mini" },
  agent_tools: 12,
  features: {},
};
const healthDegraded = {
  status: "degraded",
  database: { ok: false, url_scheme: "postgres" },
  llm: { provider: "openai", available: false, model: null },
  agent_tools: 12,
  features: {},
};

// Tool.to_info() entries (app/agent/registry.py): name, description, category, access, requires_approval.
const toolsBody = {
  count: 3,
  tools: [
    { name: "list_shops", description: "List the organization's shops.", category: "catalog", access: "read", requires_approval: false, input_schema: {} },
    { name: "crawl_shop", description: "Crawl a shop and record its changes.", category: "catalog", access: "write", requires_approval: true, input_schema: {} },
    { name: "send_email", description: null, category: "comms", access: "read", requires_approval: false, input_schema: {} },
  ],
};

// A realistic exposition: HELP/TYPE comments, a labelled counter and plain counters.
const metricsText = [
  "# HELP sparton_http_requests_total Total HTTP requests by method and status.",
  "# TYPE sparton_http_requests_total counter",
  'sparton_http_requests_total{method="GET",status="200"} 42',
  "",
  "sparton_agent_runs_total 7",
  "sparton_llm_tokens_used 1234567",
].join("\n");

// Render the system view for a signed-in non-admin; record every call as [method, href].
function renderSystem({ user = null, health = healthOk, metricsText: exposition = "", healthError, toolsError, metricsError } = {}) {
  resetEnv();
  resetDom();
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (href === "/health") return healthError ? json({ detail: healthError }, 503) : json(health);
    if (href === "/tools") return toolsError ? json({ detail: toolsError }, 500) : json(toolsBody);
    if (href === "/metrics") return metricsError ? json({ detail: metricsError }, 502) : prometheus(exposition);
    throw new Error(`unexpected fetch ${method} ${href}`);
  });
  // The view polls every 30s; the returned cleanup clears that interval, so tests must call stop().
  const stop = systemView(host, { state: { user } });
  return { host, calls, stop };
}

// Poll one macrotask at a time until the view has settled; never a fixed sleep.
async function waitFor(fn, what) {
  for (let i = 0; i < 100; i++) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error(`timed out waiting for ${what}`);
}

// All three panels have left their skeleton state (data or error rendered).
const settled = (host) => host.querySelector("[aria-busy]") === null;
const sectionWith = (host, title) => [...host.querySelectorAll("section")].find((s) => s.querySelector("h2")?.textContent === title);

test("a healthy platform shows its four metric tiles with no banner", async () => {
  const { host, stop } = renderSystem({ metricsText });
  try {
    await waitFor(() => settled(host), "the system view to settle");

    assert.equal(host.querySelector("h1").textContent, "System");
    assert.equal(host.children[0].querySelector("p").textContent, "Platform health, the agent's tool surface and workspace administration.");
    assert.ok(host.querySelector(".icon-btn"), "the OpenAPI docs button is offered");
    assert.equal(host.querySelector(".banner"), null); // nothing degraded

    const cards = [...host.querySelectorAll("article.metric")];
    assert.deepEqual(cards.map((c) => c.querySelector(".metric-label").textContent), ["Status", "Database", "LLM provider", "Agent tools"]);
    assert.deepEqual(cards.map((c) => c.querySelector(".metric-value").textContent), ["Ok", "Connected", "Available", "12"]);
    assert.equal(cards[0].querySelector(".metric-sub").textContent, "aggregate platform health");
    assert.equal(cards[1].querySelector(".metric-sub").textContent, "sqlite backend"); // url_scheme from /health
    assert.equal(cards[2].querySelector(".metric-sub").textContent, "openai · gpt-4o-mini");
    assert.equal(cards[3].querySelector(".metric-sub").textContent, "registered and callable");
    // The first three tiles carry a tone; Agent tools is neutral (no inline color).
    for (const c of cards.slice(0, 3)) assert.equal(c.querySelector(".metric-value").style.color, "var(--success)");
    assert.equal(cards[3].querySelector(".metric-value").style?.color, undefined);

    // The admin user table is only offered to admins.
    assert.equal(host.querySelector(".admin-users"), null);
  } finally {
    stop();
  }
});

test("a failing dependency shows a warning banner and red tiles", async () => {
  const { host, stop } = renderSystem({ health: healthDegraded });
  try {
    await waitFor(() => settled(host), "the system view to settle");

    const banner = host.querySelector(".banner");
    assert.equal(banner.getAttribute("data-tone"), "warning");
    assert.equal(banner.getAttribute("role"), "status");
    assert.equal(banner.querySelector("div").textContent, "The platform is degraded: one or more dependencies are unavailable.");

    const cards = [...host.querySelectorAll("article.metric")];
    assert.deepEqual(cards.map((c) => c.querySelector(".metric-value").textContent), ["Degraded", "Down", "Offline", "12"]);
    assert.equal(cards[0].querySelector(".metric-value").style.color, "var(--warning)"); // status degrades to warning
    assert.equal(cards[1].querySelector(".metric-value").style.color, "var(--danger)"); // database down is red
    assert.equal(cards[1].querySelector(".metric-sub").textContent, "postgres backend");
    assert.equal(cards[2].querySelector(".metric-value").style.color, "var(--danger)"); // LLM offline is red
    assert.equal(cards[2].querySelector(".metric-sub").textContent, "openai"); // no model configured: no dot suffix
  } finally {
    stop();
  }
});

test("the agent tools are grouped by category with access and approval badges", async () => {
  const { host, stop } = renderSystem();
  try {
    await waitFor(() => settled(host), "the system view to settle");

    const section = sectionWith(host, "Agent tools");
    assert.equal(section.querySelector(".sub").textContent, "Everything the agent is allowed to call");
    // Group headers are plain text divs: "<category> · <count>".
    const groups = [...section.querySelectorAll("div")].filter((d) => !d.querySelector("div") && d.textContent.includes(" · "));
    assert.deepEqual(groups.map((g) => g.textContent), ["catalog · 2", "comms · 1"]);

    const rows = [...section.querySelectorAll(".row")];
    assert.equal(rows.length, 3);
    assert.deepEqual(rows.map((r) => r.querySelector(".row-title").textContent), ["list_shops", "crawl_shop", "send_email"]);
    assert.equal(rows[0].querySelector(".row-sub").textContent, "List the organization's shops.");

    const read = rows[0].querySelectorAll("span.badge");
    assert.deepEqual([...read].map((b) => b.textContent), ["Read"]); // read access: one neutral badge
    assert.equal(read[0].getAttribute("data-tone"), "neutral");

    const write = [...rows[1].querySelectorAll("span.badge")];
    assert.deepEqual(write.map((b) => b.textContent), ["Write", "Approval"]); // write tools need approval
    assert.equal(write[0].getAttribute("data-tone"), "warning");
    assert.equal(write[1].getAttribute("data-tone"), "danger");

    assert.equal(rows[2].querySelector(".row-sub").textContent, "No description"); // null description falls back
  } finally {
    stop();
  }
});

test("the Prometheus exposition is parsed into name and value rows, skipping comments", async () => {
  const { host, stop } = renderSystem({ metricsText });
  try {
    await waitFor(() => settled(host), "the system view to settle");

    const section = sectionWith(host, "Counters");
    assert.equal(section.querySelector(".sub").textContent, "Live values from the Prometheus endpoint");
    const table = section.querySelector("table");
    assert.ok(table, "a table is rendered for non-empty metrics");
    assert.deepEqual([...section.querySelectorAll("th")].map((c) => c.textContent), ["Metric", "Value"]);

    const rows = [...table.querySelector("tbody").querySelectorAll("tr")];
    assert.equal(rows.length, 3); // HELP/TYPE comments and the blank line are skipped
    assert.deepEqual(
      rows.map((r) => r.querySelector("td.mono").textContent),
      ['sparton_http_requests_total{method="GET",status="200"}', "sparton_agent_runs_total", "sparton_llm_tokens_used"],
    );
    // The long name is truncated but keeps its full text in the title attribute.
    assert.equal(rows[0].querySelector("td.mono").getAttribute("title"), 'sparton_http_requests_total{method="GET",status="200"}');
    assert.deepEqual(
      rows.map((r) => r.querySelector("td.num").textContent),
      ["42", "7", "1,234,567"], // values run through num()
    );
  } finally {
    stop();
  }
});

test("no counters yet shows the empty plate instead of a table", async () => {
  const { host, stop } = renderSystem();
  try {
    await waitFor(() => settled(host), "the system view to settle");

    const section = sectionWith(host, "Counters");
    assert.equal(section.querySelector("table"), null);
    const plate = section.querySelector(".empty");
    assert.equal(plate.querySelector("h3").textContent, "No counters yet");
    assert.equal(plate.querySelector("p").textContent, "Metrics appear once requests, runs or jobs have been recorded.");
  } finally {
    stop();
  }
});

test("a failing /health shows the error state with a retry button", async () => {
  const { host, calls, stop } = renderSystem({ healthError: "database down" });
  try {
    await waitFor(() => settled(host), "the system view to settle");

    assert.deepEqual(calls.map(([, href]) => href).sort(), ["/health", "/metrics", "/tools"]); // all three panels load on mount
    const err = host.querySelector(".error-state");
    assert.equal(err.getAttribute("role"), "alert");
    assert.equal(err.querySelector("h3").textContent, t("ui.error"));
    assert.equal(err.querySelector("p").textContent, "database down"); // api.js lifts detail into the message
    assert.ok([...err.querySelectorAll("button")].some((b) => b.textContent === t("ui.retry")), "a retry button is offered");
  } finally {
    stop();
  }
});

test("a failing /metrics degrades only its own panel", async () => {
  const { host, stop } = renderSystem({ metricsText, metricsError: "scrape failed" });
  try {
    await waitFor(() => settled(host), "the system view to settle");

    // Health and tools still render their data...
    assert.equal(host.querySelectorAll("article.metric").length, 4);
    assert.equal(sectionWith(host, "Agent tools").querySelectorAll(".row").length, 3);
    // ...while the counters panel shows its error state.
    const err = sectionWith(host, "Counters").querySelector(".error-state");
    assert.equal(err.getAttribute("role"), "alert");
    assert.equal(err.querySelector("p").textContent, "scrape failed");
  } finally {
    stop();
  }
});

