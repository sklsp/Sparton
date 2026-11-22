// system.js on the DOM fake: the four health tiles from /health, how a failing dependency is
// reported (warning banner plus red tiles), the agent tool registry grouped by category with its
// access and approval badges, the Prometheus exposition parsed into name/value rows, the empty
// counters plate, each panel's error state, the Counters refresh button and the admin-only user
// roster: you chip, role and status badges, the Disable/Enable toggle (success toast plus reload,
// refused-toggle danger toast) and its empty plate. Fixtures mirror GET /health, GET /tools and
// GET /metrics from app/api/health.py (tool entries as Tool.to_info() in app/agent/registry.py)
// and GET/PATCH /admin/users from app/api/admin.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import systemView from "../../app/web/views/system.js";
import { t } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
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

// The shapes exactly as admin.py builds them: GET /admin/users returns { count, users } sorted by
// id; PATCH /admin/users/{id} flips is_active and 409s when it would leave an org without an active admin.
const usersBody = {
  count: 3,
  users: [
    { id: 3, email: "jane@acme.test", role: "admin", is_active: true, organization_id: 1 },
    { id: 4, email: "bob@acme.test", role: "member", is_active: false, organization_id: 1 },
    { id: 7, email: "admin@acme.test", role: "admin", is_active: true, organization_id: 1 },
  ],
};
const adminUser = { id: 7, role: "admin" }; // state.user as /auth/me returns it; the view reads .role and .id only

// Render the system view for a signed-in non-admin; record every call as [method, href, body].
function renderSystem({ user = null, health = healthOk, metricsText: exposition = "", users = structuredClone(usersBody), patchError, healthError, toolsError, metricsError } = {}) {
  resetEnv();
  resetDom();
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href, init?.body]);
    if (href === "/health") return healthError ? json({ detail: healthError }, 503) : json(health);
    if (href === "/tools") return toolsError ? json({ detail: toolsError }, 500) : json(toolsBody);
    if (href === "/metrics") return metricsError ? json({ detail: metricsError }, 502) : prometheus(exposition);
    if (method === "PATCH" && href.startsWith("/admin/users/")) {
      const id = Number(href.split("/").pop());
      const target = users.users.find((u) => u.id === id);
      if (!target) throw new Error(`unexpected fetch ${method} ${href}`);
      if (patchError) return json({ detail: patchError }, 409);
      target.is_active = !target.is_active; // the real endpoint flips and persists it, so a later GET reflects it
      return json(target);
    }
    if (href === "/admin/users") return json(users);
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
  const { host, calls, stop } = renderSystem({ metricsText });
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

    // The admin user table is only offered to admins... and never fetched either.
    assert.equal(sectionWith(host, "Users"), undefined);
    assert.ok(!calls.some(([, href]) => href === "/admin/users"));
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

test("the Counters refresh button re-fetches /metrics and shows its busy state", async () => {
  const { host, calls, stop } = renderSystem({ metricsText });
  try {
    await waitFor(() => settled(host), "the system view to settle");
    assert.equal(calls.filter(([, href]) => href === "/metrics").length, 1); // the mount fetch

    const refresh = host.querySelector('button.icon-btn[aria-label="Refresh metrics"]');
    assert.ok(refresh, "a refresh button sits in the Counters head");
    refresh.click();
    assert.equal(refresh.dataset.busy, "true"); // busy until the 600ms guard clears it

    await waitFor(() => calls.filter(([, href]) => href === "/metrics").length === 2, "the refresh fetch to land");
    await waitFor(() => settled(host), "the view to settle after the refresh");
    assert.equal([...host.querySelectorAll(".table-wrap tbody tr")].length, 3); // re-rendered from the same exposition
  } finally {
    stop();
  }
});

test("an admin sees the user roster with you chip, role and status badges", async () => {
  const { host, calls, stop } = renderSystem({ user: adminUser });
  try {
    await waitFor(() => settled(host), "the system view to settle");
    assert.ok(calls.some(([, href]) => href === "/admin/users"), "the roster is fetched for admins");

    const section = sectionWith(host, "Users");
    assert.equal(section.querySelector(".sub").textContent, "Members of this organization");
    assert.deepEqual([...section.querySelectorAll("th")].map((c) => c.textContent), ["Email", "Role", "Status", ""]);

    const rows = [...section.querySelectorAll("tbody tr")];
    assert.equal(rows.length, 3); // sorted by id: 3, 4, 7
    assert.deepEqual(
      rows.map((r) => r.querySelector("td").textContent),
      ["jane@acme.test", "bob@acme.test", "admin@acme.testyou"], // the signed-in admin (id 7) gets the chip
    );
    const you = section.querySelector(".chip");
    assert.equal(you.textContent, "you");

    const roleBadges = rows.map((r) => r.querySelectorAll("td")[1].querySelector(".badge"));
    assert.deepEqual(
      roleBadges.map((b) => [b.textContent, b.getAttribute("data-tone")]),
      [["Admin", "primary"], ["Member", "neutral"], ["Admin", "primary"]],
    );
    const statusBadges = rows.map((r) => r.querySelectorAll("td")[2].querySelector(".badge"));
    assert.deepEqual(
      statusBadges.map((b) => [b.textContent, b.getAttribute("data-tone")]),
      [["Active", "success"], ["Disabled", "neutral"], ["Active", "success"]],
    );

    // Only the other users get a toggle; the signed-in admin's row has none.
    const buttons = [...section.querySelectorAll("button.btn")];
    assert.deepEqual(buttons.map((b) => b.textContent), ["Disable", "Enable"]);
    assert.equal(buttons[0].getAttribute("data-variant"), "danger"); // disabling is destructive
    assert.equal(buttons[1].getAttribute("data-size"), "sm");
  } finally {
    stop();
  }
});

test("disabling a user PATCHes the endpoint, toasts and reloads the roster", async () => {
  const { host, calls, stop } = renderSystem({ user: adminUser });
  resetToasts();
  try {
    await waitFor(() => settled(host), "the system view to settle");

    const section = sectionWith(host, "Users");
    const disable = [...section.querySelectorAll("button.btn")].find((b) => b.textContent === "Disable"); // jane (id 3)
    disable.click();
    assert.equal(disable.dataset.loading, "true"); // the button shows its in-flight state

    await waitFor(() => calls.some(([method, href]) => method === "PATCH" && href === "/admin/users/3"), "the PATCH to land");
    const [, , body] = calls.find(([method, href]) => method === "PATCH" && href === "/admin/users/3");
    assert.deepEqual(JSON.parse(body), { is_active: false });

    await waitFor(() => document.querySelector(".toast"), "the success toast");
    const toastEl = document.querySelector(".toast");
    assert.equal(toastEl.getAttribute("data-tone"), "success");
    assert.equal(toastEl.querySelector("div").textContent, "User disabled");

    // The reload re-renders the roster: jane's row now offers Enable and shows Disabled.
    await waitFor(() => {
      const btns = [...section.querySelectorAll("button.btn")].map((b) => b.textContent);
      return btns.length === 2 && btns[0] === "Enable";
    }, "the roster to reload with jane disabled");
    assert.equal(calls.filter(([, href]) => href === "/admin/users").length, 2); // one GET per render
    const buttons = [...section.querySelectorAll("button.btn")];
    assert.deepEqual(buttons.map((b) => b.textContent), ["Enable", "Enable"]); // jane is now disabled too
    assert.equal(section.querySelectorAll("tbody tr")[0].querySelectorAll("td")[2].textContent, "Disabled");
  } finally {
    stop();
  }
});

test("a refused toggle shows a danger toast and leaves the roster untouched", async () => {
  const { host, calls, stop } = renderSystem({ user: adminUser, patchError: "Cannot disable the last active admin" });
  resetToasts();
  try {
    await waitFor(() => settled(host), "the system view to settle");

    const section = sectionWith(host, "Users");
    const enable = [...section.querySelectorAll("button.btn")].find((b) => b.textContent === "Enable"); // bob (id 4)
    enable.click();
    assert.equal(enable.dataset.loading, "true");

    await waitFor(() => calls.some(([method]) => method === "PATCH"), "the PATCH to land");
    // The refusal surfaces as a danger toast; catch clears the loading flag right after.
    await waitFor(() => enable.dataset.loading === undefined, "the button to recover");
    const toastEl = document.querySelector(".toast");
    assert.equal(toastEl.getAttribute("data-tone"), "danger");
    assert.equal(toastEl.querySelector("div").textContent, "Cannot disable the last active admin");

    // The failed toggle does not reload the roster.
    assert.equal(calls.filter(([, href]) => href === "/admin/users").length, 1); // no second GET after a failure
    assert.deepEqual([...section.querySelectorAll("button.btn")].map((b) => b.textContent), ["Disable", "Enable"]);
  } finally {
    stop();
  }
});

test("an org without users shows the empty plate instead of a table", async () => {
  const { host, stop } = renderSystem({ user: adminUser, users: { count: 0, users: [] } });
  try {
    await waitFor(() => settled(host), "the system view to settle");

    const section = sectionWith(host, "Users");
    assert.equal(section.querySelector("table"), null);
    const plate = section.querySelector(".empty");
    assert.equal(plate.querySelector("h3").textContent, "No users found");
  } finally {
    stop();
  }
});

