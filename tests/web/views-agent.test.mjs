// agent.js on the DOM fake: starting a run (the POST /agent/run body), the runs table, one
// run's step trace, approving or rejecting pending approvals (what each request sends), and
// empty/error states. Fixtures mirror the response shapes in app/api/agent_api.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import agentView from "../../app/web/views/agent.js";
import { t } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom, Event } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// Fixtures mirror the shapes agent_api.py builds. Timestamps are relative so ago()/when() stay stable.
const NOW = Date.now();
const iso = (msAgo) => new Date(NOW - msAgo).toISOString().replace("T", " ").slice(0, 19);

const runQueued = { id: 7, request: "Audit the pricing page copy", status: "PENDING", iterations: 0, tool_calls_made: 0, started_at: iso(12e3) };
const runLive = { id: 6, request: "Refresh the hero images", status: "RUNNING", iterations: 2, tool_calls_made: 1, started_at: iso(95e3) };
const runDone = { id: 5, request: "Find products with weak descriptions and propose better copy", status: "COMPLETED", iterations: 4, tool_calls_made: 3, started_at: iso(60 * 60e3), final_response: "Rewrote the description for SKU-204." };
const runsBody = { count: 3, runs: [runQueued, runLive, runDone] };

// The detail endpoint returns more than the list rows do.
const liveDetail = { ...runLive, steps: [
  { step_number: 0, type: "request", message: "Refresh the hero images", status: "ok" },
  { step_number: 1, type: "decision", message: "Plan: read_pages → update_product", status: "ok" },
  { step_number: 2, type: "tool_call", tool: "update_product", message: "Uploading the new hero image…", status: "pending" },
] };
const doneDetail = { ...runDone, steps: [
  { step_number: 0, type: "request", message: runDone.request, status: "ok" },
  { step_number: 1, type: "decision", message: "Plan: list_products → read_pages → update_product", status: "ok" },
  { step_number: 2, type: "tool_call", tool: "list_products", message: "Fetched 48 products", status: "ok" },
  { step_number: 3, type: "approval_request", tool: "update_product", message: "Proposed copy for SKU-204", status: "ok" },
] };

const approvalPending = { id: 41, run_id: 6, tool: "update_product", summary: "Rewrite the hero description on product 8842", status: "PENDING", preview: { changes: [{ field: "description", current: "Nice shoes.", proposed: "Hand-finished leather sneakers with a matte sole." }] }, created_at: iso(30e3) };
const approvalApproved = { id: 40, run_id: 5, tool: "update_product", summary: "Rewrite the hero description on product 8841", status: "APPROVED", preview: null, created_at: iso(60 * 60e3) };
const approvalsBody = { count: 2, approvals: [approvalPending, approvalApproved] };


// Render the agent view; record every call as [method, href] and every POST as [href, body].
function renderAgent({ runs = runsBody, approvals = approvalsBody, failRunsOnce = false, startResponse = null } = {}) {
  resetEnv();
  resetDom();
  resetToasts(); // ui.js caches its toasts host; a fresh document needs a fresh one.
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  const posts = [];
  let runsFailuresLeft = failRunsOnce ? 1 : 0;
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (method === "POST" && href === "/agent/run") {
      posts.push([href, JSON.parse(init.body)]);
      return startResponse ?? json({ run_id: 7 }, 201);
    }
    const resolveMatch = href.match(/^\/approvals\/(\d+)\/resolve$/);
    if (method === "POST" && resolveMatch) {
      const body = JSON.parse(init.body);
      posts.push([href, body]);
      return json({ ok: true, status: body.approved ? "APPROVED" : "REJECTED" });
    }
    if (href === "/agent/runs?limit=25") {
      if (runsFailuresLeft > 0) { runsFailuresLeft -= 1; return json({ detail: "Agent runs unavailable" }, 503); }
      return json(runs);
    }
    const runMatch = href.match(/^\/agent\/runs\/(\d+)$/);
    if (runMatch) {
      const id = Number(runMatch[1]);
      if (id === 7) return json({ ...runQueued, steps: [] }); // a queued run has no trace yet
      if (id === 6) return json(liveDetail);
      if (id === 5) return json(doneDetail);
    }
    if (href.startsWith("/approvals")) {
      const filter = new URLSearchParams(href.split("?")[1] ?? "").get("status_filter"); // mirrors the API's status filter
      const items = filter
        ? approvals.approvals.filter((a) => a.status === filter)
        : approvals.approvals.filter((a) => a.status === "PENDING");
      return json({ count: items.length, approvals: items });
    }
    throw new Error(`unexpected fetch ${method} ${href}`);
  });
  const badgeCalls = [];
  // The view polls the open run every 2s and the approval queue every 20s; stop() clears both.
  const stop = agentView(host, { refreshApprovalBadge: () => badgeCalls.push(1) });
  return { host, calls, posts, badgeCalls, stop };
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
const runRows = (host) => [...sectionWith(host, "Runs").querySelectorAll("tbody tr")];
const approvalRows = (host) => [...sectionWith(host, "Approvals").querySelectorAll(".row")];

test("mounting loads the runs table and the pending approval queue", async () => {
  const { host, calls, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");

    assert.equal(host.querySelector("h1").textContent, "Agent");
    assert.equal(host.querySelector(".page-head p").textContent, "Athena runs with tool access. Every write pauses for your approval before it lands.");
    const gets = [...new Set(calls.filter(([m]) => m === "GET").map(([, h]) => h))].sort();
    assert.deepEqual(gets, ["/agent/runs?limit=25", "/approvals"]);
    assert.deepEqual([...host.querySelectorAll("section.card h2")].map((h) => h.textContent), ["New run", "Approvals", "Runs"]);

    const rows = runRows(host);
    assert.equal(rows.length, 3);
    assert.deepEqual(rows.map((r) => r.querySelector(".truncate").textContent), [runQueued.request, runLive.request, runDone.request]);
    const badges = rows.map((r) => r.querySelector("td .badge"));
    assert.deepEqual(badges.map((b) => b.textContent), ["PENDING", "RUNNING", "COMPLETED"]);
    // Queued and running runs get the pulsing dot; finished ones do not.
    assert.ok(badges[0].querySelector(".dot-live"), "the queued run is live");
    assert.ok(badges[1].querySelector(".dot-live"), "the running run is live");
    assert.equal(badges[2].querySelector(".dot-live"), null);
    assert.deepEqual(rows.map((r) => r.querySelector("td.num").textContent), ["0", "1", "3"]);

    const queue = approvalRows(host);
    assert.equal(queue.length, 1); // only pending approvals land in the default view
    assert.equal(queue[0].querySelector(".row-title").textContent, approvalPending.summary);
    // created_at is 30s back; ago() renders anything under 45s as "just now".
    assert.equal(queue[0].querySelector(".row-sub").textContent, "update_product · run #6 · just now");
    assert.deepEqual([...queue[0].querySelectorAll("button")].map((b) => b.textContent), ["Preview", "Reject", "Approve"]);
  } finally {
    stop();
  }
});

test("an empty request is refused without any request", async () => {
  const { host, calls, posts, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");
    host.querySelector("form").dispatchEvent(new Event("submit"));
    await new Promise((resolve) => setImmediate(resolve)); // give the handler a turn; nothing may leave the browser
    assert.equal(posts.length, 0);
    assert.equal(calls.filter(([m]) => m === "POST").length, 0);
    assert.equal(document.querySelector(".toast"), null);
  } finally {
    stop();
  }
});

test("starting a run posts the message and opens its live feed", async () => {
  const { host, calls, posts, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");
    const form = host.querySelector("form");
    const input = form.querySelector('textarea[aria-label="Agent request"]');
    assert.equal(input.getAttribute("maxlength"), "8000"); // the client enforces the API's limit
    input.value = "  Audit the pricing page copy  ";

    form.dispatchEvent(new Event("submit"));
    await waitFor(() => posts.length === 1, "the POST to land");
    assert.deepEqual(posts[0], ["/agent/run", { message: "Audit the pricing page copy", session_id: "dashboard" }]); // trimmed; default session

    await waitFor(() => document.querySelector('.toast[data-tone="success"]'), "the success toast");
    assert.equal(document.querySelector('.toast[data-tone="success"]').querySelector("div").textContent, "Run #7 queued");
    assert.equal(input.value, ""); // the form clears on success

    const panel = sectionWith(host, "Run #7");
    await waitFor(() => panel?.querySelector(".empty"), "the empty step feed to render");
    assert.equal(panel.querySelector(".sub").textContent, "Live step feed");
    assert.equal(panel.querySelector(".empty h3").textContent, "Waiting for the first step");
    const badge = panel.querySelector(".card-head-actions .badge");
    assert.equal(badge.textContent, "PENDING");
    assert.ok(badge.querySelector(".dot-live"), "a queued run is still live");

    // The runs table reloaded with the new run on top.
    await waitFor(() => calls.filter(([m, h]) => m === "GET" && h === "/agent/runs?limit=25").length >= 2, "the runs list to reload");
    assert.equal(runRows(host)[0].querySelector(".truncate").textContent, runQueued.request);
  } finally {
    stop();
  }
});



test("opening a finished run shows its full step trace", async () => {
  const { host, calls, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");
    runRows(host)[2].click(); // the COMPLETED run
    await waitFor(() => calls.some(([m, h]) => m === "GET" && h === "/agent/runs/5"), "the detail fetch");

    // openRun first fills a skeleton card ("Loading trace…") and then swaps in the real panel;
    // always re-query from the live tree so we never hold onto the replaced node.
    const panel = () => sectionWith(host, "Run #5");
    await waitFor(() => panel().querySelectorAll(".tl-item").length === 4, "all four steps to render");

    assert.equal(panel().querySelector(".sub").textContent, "Live step feed");
    const items = [...panel().querySelectorAll(".tl-item")];
    assert.deepEqual(items.map((el) => el.getAttribute("data-tone")), ["success", "success", "success", "success"]);
    assert.deepEqual(items.map((el) => el.querySelector(".tl-title").textContent), doneDetail.steps.map((s) => s.message));
    // Each step carries its type badge and, for tool steps, the tool name.
    assert.deepEqual(items.map((el) => el.querySelector(".tl-meta .badge").textContent), ["Request", "Decision", "Tool Call", "Approval Request"]);
    assert.deepEqual(items.map((el) => el.querySelector(".chip")?.textContent ?? null), [null, null, "list_products", "update_product"]);

    const foot = panel().querySelector("footer.card-foot");
    assert.match(foot.textContent, /4 iterations/);
    assert.match(foot.textContent, /3 tool calls/);
    assert.ok([...foot.querySelectorAll("span")].some((s) => s.textContent.startsWith("started ")), "the start time is shown");
    // The final response renders as its own block.
    assert.equal(panel().querySelector(".msg-text").textContent, runDone.final_response);
  } finally {
    stop();
  }
});

test("opening a live run shows its in-flight step as pending", async () => {
  const { host, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");
    runRows(host)[1].click(); // the RUNNING run
    const panel = () => sectionWith(host, "Run #6"); // re-queried: openRun swaps in a fresh node once loaded
    await waitFor(() => panel().querySelectorAll(".tl-item").length === 3, "all three steps to render");

    assert.equal(panel().querySelector(".sub").textContent, "Live step feed");
    const items = [...panel().querySelectorAll(".tl-item")];
    assert.equal(items[2].getAttribute("data-tone"), "warning"); // the in-flight tool call is pending
    assert.equal(panel().querySelector(".msg-text"), null); // no final response yet
    const badge = panel().querySelector(".card-head-actions .badge");
    assert.equal(badge.textContent, "RUNNING");
    assert.ok(badge.querySelector(".dot-live"));
  } finally {
    stop();
  }
});

test("approving a pending approval resolves it and refreshes the badge", async () => {
  const { host, calls, posts, badgeCalls, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");
    const row = approvalRows(host)[0];
    [...row.querySelectorAll("button")].find((b) => b.textContent === "Approve").click();

    await waitFor(() => posts.length === 1, "the resolve POST to land");
    assert.deepEqual(posts[0], ["/approvals/41/resolve", { approved: true, note: null }]);

    await waitFor(() => document.querySelector('.toast[data-tone="success"]'), "the success toast");
    assert.equal(document.querySelector('.toast[data-tone="success"]').querySelector("div").textContent, "Approved. The run is resuming");
    assert.ok(badgeCalls.length >= 1, "the nav badge is refreshed");
    await waitFor(() => calls.filter(([m, h]) => m === "GET" && h === "/approvals").length >= 2, "the queue to reload");
  } finally {
    stop();
  }
});

test("rejecting asks for confirmation and cancelling sends nothing", async () => {
  const { host, posts, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");
    const row = approvalRows(host)[0];
    [...row.querySelectorAll("button")].find((b) => b.textContent === "Reject").click();

    const dlg = document.querySelector("dialog");
    assert.ok(dlg, "a confirmation dialog opens");
    assert.equal(dlg.querySelector("#dlg-title").textContent, "Reject this write?");
    assert.equal(dlg.querySelector(".dialog-body p").textContent, "update_product will be skipped and the run will continue without it.");

    const cancel = [...dlg.querySelectorAll(".dialog-foot button")].find((b) => b.textContent === t("ui.cancel"));
    cancel.click();
    await new Promise((resolve) => setImmediate(resolve)); // let the promise settle; nothing may leave the browser
    assert.equal(posts.length, 0);
    assert.equal(document.querySelector("dialog"), null); // the dialog is gone
  } finally {
    stop();
  }
});

test("confirming a rejection posts approved:false", async () => {
  const { host, posts, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");
    const row = approvalRows(host)[0];
    [...row.querySelectorAll("button")].find((b) => b.textContent === "Reject").click();

    const dlg = document.querySelector("dialog");
    const confirm = [...dlg.querySelectorAll(".dialog-foot button")].find((b) => b.textContent === "Reject");
    assert.equal(confirm.getAttribute("data-variant"), "danger"); // the destructive action is styled as such
    confirm.click();

    await waitFor(() => posts.length === 1, "the resolve POST to land");
    assert.deepEqual(posts[0], ["/approvals/41/resolve", { approved: false, note: null }]);
    await waitFor(() => document.querySelector('.toast[data-tone="info"]'), "the info toast");
    assert.equal(document.querySelector('.toast[data-tone="info"]').querySelector("div").textContent, "Rejected");
  } finally {
    stop();
  }
});

test("the status tabs filter the approval queue", async () => {
  const { host, calls, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");
    const section = sectionWith(host, "Approvals");
    const tabs = [...section.querySelectorAll(".tabs button")];
    assert.deepEqual(tabs.map((b) => b.textContent), ["Pending", "Approved", "Rejected"]);
    assert.deepEqual(tabs.map((b) => b.getAttribute("aria-selected")), ["true", "false", "false"]);

    tabs[1].click(); // Approved
    await waitFor(() => calls.some(([m, h]) => m === "GET" && h === "/approvals?status_filter=APPROVED"), "the filtered fetch");
    await waitFor(() => approvalRows(host).length === 1, "the filtered queue to render");
    const rows = approvalRows(host);
    assert.equal(rows.length, 1);
    assert.equal(rows[0].querySelector(".row-title").textContent, approvalApproved.summary);
    assert.deepEqual([...rows[0].querySelectorAll("button")].map((b) => b.textContent), []); // resolved: no actions left
    const badge = rows[0].querySelector(".badge");
    assert.equal(badge.textContent, "APPROVED");
    assert.deepEqual(tabs.map((b) => b.getAttribute("aria-selected")), ["false", "true", "false"]);
  } finally {
    stop();
  }
});

test("empty runs and approvals show their empty states", async () => {
  const { host, stop } = renderAgent({ runs: { count: 0, runs: [] }, approvals: { count: 0, approvals: [] } });
  try {
    await waitFor(() => settled(host), "the panels to settle");
    assert.equal(sectionWith(host, "Runs").querySelector(".empty h3").textContent, "No runs yet");
    assert.equal(sectionWith(host, "Approvals").querySelector(".empty h3").textContent, "Nothing waiting on you");
  } finally {
    stop();
  }
});

test("a failed runs load shows an error state whose retry recovers", async () => {
  const { host, stop } = renderAgent({ failRunsOnce: true });
  try {
    await waitFor(() => settled(host), "the panels to settle");
    const section = sectionWith(host, "Runs");
    const err = section.querySelector(".error-state");
    assert.ok(err, "the runs panel reports the failure instead of a skeleton");
    assert.equal(err.getAttribute("role"), "alert");
    assert.equal(err.querySelector("h3").textContent, t("ui.error"));
    assert.equal(err.querySelector("p").textContent, "Agent runs unavailable");

    const retry = [...err.querySelectorAll("button")].find((b) => b.textContent === t("ui.retry"));
    assert.ok(retry, "a retry button is offered");
    retry.click();
    await waitFor(() => runRows(host).length === 3, "the runs to reload after retry");
    assert.equal(section.querySelector(".error-state"), null);
  } finally {
    stop();
  }
});

