// The branch sides of system.js that views-system.test.mjs does not reach: /health bodies without url_scheme or llm provider (the "unknown" fallbacks), a /tools body without a tools list (empty plate), tools without category, description or access (general group, no badges), a /admin/users body without a users list (empty plate), and the success path of enabling a disabled user (toast + roster reload). Fixtures mirror GET /health, GET /tools and GET/PATCH /admin/users from app/api/health.py and app/api/admin.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import systemView from "../../app/web/views/system.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
// /metrics is served as text/plain; version=0.0.4, so api.metrics() returns the raw exposition.
const prometheus = (body, status = 200) => new Response(body, { status, headers: { "content-type": "text/plain; version=0.0.4" } });

// The shape exactly as admin.py builds it: GET /admin/users returns { count, users };
// PATCH /admin/users/{id} flips is_active and persists it, so a later GET reflects the flip.
const usersFixture = {
  count: 2,
  users: [
    { id: 4, email: "bob@acme.test", role: "member", is_active: false, organization_id: 1 },
    { id: 7, email: "admin@acme.test", role: "admin", is_active: true, organization_id: 1 },
  ],
};
const adminUser = { id: 7, role: "admin" }; // state.user as /auth/me returns it; the view reads .role and .id only

// Render the system view against scripted routes; record every call as [method, href, body].
function renderSystem({ user = null, health = { status: "ok", database: { ok: true }, llm: { available: false, model: null }, agent_tools: 0, features: {} }, tools = { count: 0, tools: [] }, usersBody = null } = {}) {
  resetEnv();
  resetDom();
  resetToasts(); // ui.js caches its toasts host; a fresh document needs a fresh one.
  const users = structuredClone(usersBody ?? usersFixture);
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href, init?.body]);
    if (href === "/health") return json(health);
    if (href === "/tools") return json(tools);
    if (href === "/metrics") return prometheus("");
    if (method === "PATCH" && href.startsWith("/admin/users/")) {
      const id = Number(href.split("/").pop());
      const target = users.users.find((u) => u.id === id);
      if (!target) throw new Error(`unexpected fetch ${method} ${href}`);
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

test("a /health body without url_scheme or llm provider falls back to unknown", async () => {
  const { host, stop } = renderSystem({ health: { status: "ok", database: { ok: true }, llm: { available: false, model: null }, agent_tools: 0, features: {} } });
  try {
    await waitFor(() => settled(host), "the system view to settle");

    const cards = [...host.querySelectorAll("article.metric")];
    assert.equal(cards[1].querySelector(".metric-sub").textContent, "unknown backend"); // no url_scheme in the body
    assert.equal(cards[2].querySelector(".metric-value").textContent, "Offline");
    assert.equal(cards[2].querySelector(".metric-sub").textContent, "unknown"); // no provider and no model to append
  } finally {
    stop();
  }
});

test("a /tools body without a tools list shows the empty plate", async () => {
  const { host, stop } = renderSystem({ tools: { count: 0 } });
  try {
    await waitFor(() => settled(host), "the system view to settle");

    const section = sectionWith(host, "Agent tools");
    assert.equal(section.querySelector(".row"), null);
    const plate = section.querySelector(".empty");
    assert.equal(plate.querySelector("h3").textContent, "No tools registered");
  } finally {
    stop();
  }
});

test("a tool without category, description or access lands in general with no badges", async () => {
  const { host, stop } = renderSystem({ tools: { count: 2, tools: [
    { name: "mystery_tool", description: null },
    { name: "list_shops", description: "List the organization's shops.", category: "catalog", access: "read", requires_approval: false, input_schema: {} },
  ] } });
  try {
    await waitFor(() => settled(host), "the system view to settle");

    const section = sectionWith(host, "Agent tools");
    // The uncategorised tool is grouped under the general fallback.
    const groups = [...section.querySelectorAll("div")].filter((d) => !d.querySelector("div") && d.textContent.includes(" · "));
    assert.deepEqual(groups.map((g) => g.textContent), ["general · 1", "catalog · 1"]);

    const rows = [...section.querySelectorAll(".row")];
    assert.equal(rows[0].querySelector(".row-sub").textContent, "No description"); // null description fallback
    assert.equal(rows[0].querySelectorAll("span.badge").length, 0); // no access and no approval: nothing to badge
    assert.deepEqual([...rows[1].querySelectorAll("span.badge")].map((b) => b.textContent), ["Read"]);
  } finally {
    stop();
  }
});

test("a /admin/users body without a users list shows the empty plate", async () => {
  const { host, stop } = renderSystem({ user: adminUser, usersBody: { count: 0 } });
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

test("enabling a disabled user succeeds with its toast and reloads the roster", async () => {
  const { host, calls, stop } = renderSystem({ user: adminUser });
  try {
    await waitFor(() => settled(host), "the system view to settle");

    const section = sectionWith(host, "Users");
    const enable = [...section.querySelectorAll("button.btn")].find((b) => b.textContent === "Enable"); // bob (id 4)
    enable.click();
    assert.equal(enable.dataset.loading, "true");

    await waitFor(() => calls.some(([method, href]) => method === "PATCH" && href === "/admin/users/4"), "the PATCH to land");
    const [, , body] = calls.find(([method, href]) => method === "PATCH" && href === "/admin/users/4");
    assert.deepEqual(JSON.parse(body), { is_active: true });

    await waitFor(() => document.querySelector(".toast"), "the success toast");
    const toastEl = document.querySelector(".toast");
    assert.equal(toastEl.getAttribute("data-tone"), "success");
    assert.equal(toastEl.querySelector("div").textContent, "User enabled");

    // The reload re-renders the roster: bob is active again, so his row offers Disable;
    // the admin's own row never gets a button (isSelf).
    await waitFor(() => {
      const btns = [...section.querySelectorAll("button.btn")].map((b) => b.textContent);
      return btns.length === 1 && btns[0] === "Disable";
    }, "the roster to reload with bob enabled");
    assert.equal(calls.filter(([, href]) => href === "/admin/users").length, 2); // one GET per render
  } finally {
    stop();
  }
});
