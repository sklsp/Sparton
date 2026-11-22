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

// A run that failed with a message; the detail endpoint carries it in `error` like agent_api.py does.
const runFailed = { id: 8, request: "Sync the competitor price sheet", status: "FAILED", iterations: 1, tool_calls_made: 0, started_at: iso(5 * 60e3), error: "The worker crashed mid-step" };

// A run that vanished server-side; the detail endpoint answers 404 like agent_api.py does.
const runsGone = { count: 1, runs: [{ id: 9, request: "Ghost run", status: "FAILED", iterations: 1, tool_calls_made: 0, started_at: iso(2 * 60e3) }] };

// The live run's trace as it finishes; the poll sequence hands these out in order.
const liveDone = { ...runLive, status: "COMPLETED", iterations: 3, tool_calls_made: 2, final_response: "Hero images refreshed." };

// An approval whose preview has no changes table; showPreview falls back to raw JSON.
const approvalNote = { id: 42, run_id: 6, tool: "update_product", summary: "Bulk price update for the winter range", status: "PENDING", preview: { note: "12 products repriced" }, created_at: iso(10e3) };

// The tool registry mirrors health.py's /tools: to_info() fields from app/agent/registry.py.
const toolsBody = { count: 2, tools: [
  { name: "list_products", description: "List the organisation's products.", category: "catalogue", access: "read", requires_approval: false },
  { name: "update_product", description: "Update a product's fields.", category: "catalogue", access: "write", requires_approval: true },
] };


// Render the agent view; record every call as [method, href] and every POST as [href, body].
function renderAgent({ runs = runsBody, approvals = approvalsBody, failRunsOnce = false, startResponse = null, liveRunStates = [liveDetail], tools = toolsBody, failToolsOnce = false, failResolveOnce = false } = {}) {
  resetEnv();
  resetDom();
  resetToasts(); // ui.js caches its toasts host; a fresh document needs a fresh one.
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  const posts = [];
  let runsFailuresLeft = failRunsOnce ? 1 : 0;
  let resolveFailuresLeft = failResolveOnce ? 1 : 0;
  let toolFailuresLeft = failToolsOnce ? 1 : 0;
  let liveStateIndex = 0; // the open run's detail answers in sequence, like a run progressing
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
      if (resolveFailuresLeft > 0) { resolveFailuresLeft -= 1; return json({ detail: "Approval already resolved" }, 409); }
      return json({ ok: true, status: body.approved ? "APPROVED" : "REJECTED" });
    }
    if (href === "/agent/runs?limit=25") {
      if (runsFailuresLeft > 0) { runsFailuresLeft -= 1; return json({ detail: "Agent runs unavailable" }, 503); }
      return json(runs);
    }
    if (href === "/tools") {
      if (toolFailuresLeft > 0) { toolFailuresLeft -= 1; return json({ detail: "Tool registry unavailable" }, 503); }
      return json(tools);
    }
    const runMatch = href.match(/^\/agent\/runs\/(\d+)$/);
    if (runMatch) {
      const id = Number(runMatch[1]);
      if (id === 9) return json({ detail: "Run not found" }, 404); // the run vanished server-side
      if (id === 8) return json({ ...runFailed, steps: [] });
      if (id === 7) return json({ ...runQueued, steps: [] }); // a queued run has no trace yet
      if (id === 6) { const state = liveRunStates[Math.min(liveStateIndex, liveRunStates.length - 1)]; liveStateIndex += 1; return typeof state === "string" ? json({ detail: state }, 503) : json(state); } // string entries are outages
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

test("opening a vanished run shows the error card; Try again refetches", async () => {
  const { host, calls, stop } = renderAgent({ runs: runsGone });
  try {
    await waitFor(() => settled(host), "the panels to settle");
    runRows(host)[0].click(); // row click opens the run (renderRuns wires onclick)

    const panel = () => sectionWith(host, "Run #9");
    await waitFor(() => panel()?.querySelector(".error-state"), "the error card");
    assert.equal(panel().querySelector(".error-state h3").textContent, t("ui.error"));
    assert.equal(panel().querySelector(".error-state p").textContent, "Run not found");

    const detailCalls = () => calls.filter(([m, href]) => m === "GET" && href === "/agent/runs/9").length;
    assert.equal(detailCalls(), 1);
    panel().querySelector(".error-state .btn").click(); // the retry button refetches
    await waitFor(() => detailCalls() >= 2, "the refetch");
    await waitFor(() => panel()?.querySelector(".error-state"), "the error card to come back");
    assert.equal(detailCalls(), 2);
  } finally { stop(); }
});

test("a live run keeps polling until it finishes; the summary fills in", async () => {
  const { host, calls, badgeCalls, stop } = renderAgent({ liveRunStates: [liveDetail, liveDone] });
  try {
    await waitFor(() => settled(host), "the panels to settle");
    runRows(host)[1].click(); // the RUNNING row

    const panel = () => sectionWith(host, "Run #6");
    await waitFor(() => panel()?.querySelectorAll(".tl-item").length === 3, "the first steps");
    assert.equal(panel().querySelector(".badge").textContent, "RUNNING");
    assert.ok(panel().querySelector(".dot-live"), "the live dot while running");

    // The 2s poll sees the finished state; finish() refetches and fires onDone.
    await waitFor(() => panel()?.querySelector(".msg-text")?.textContent === liveDone.final_response, "the final response");
    assert.equal(panel().querySelector(".badge").textContent, "COMPLETED");
    assert.equal(panel().querySelector(".dot-live"), null); // the dot is gone once finished
    const foot = [...panel().querySelectorAll("footer span")].map((s) => s.textContent);
    assert.ok(foot.includes("3 iterations"));
    assert.ok(foot.includes("2 tool calls"));

    const detailCalls = () => calls.filter(([m, href]) => m === "GET" && href === "/agent/runs/6").length;
    assert.equal(detailCalls(), 3); // open, poll, finish refetch
    const listCalls = () => calls.filter(([m, href]) => m === "GET" && href === "/agent/runs?limit=25").length;
    assert.equal(listCalls(), 2); // mount plus onDone's reloadRuns
    assert.equal(badgeCalls.length, 1); // onDone refreshed the approval badge
  } finally { stop(); }
});

test("a failed poll stops following the run; the last known state stays", async () => {
  const { host, calls, stop } = renderAgent({ liveRunStates: [liveDetail, "Agent runs unavailable"] });
  try {
    await waitFor(() => settled(host), "the panels to settle");
    runRows(host)[1].click(); // the RUNNING row

    const panel = () => sectionWith(host, "Run #6");
    await waitFor(() => panel()?.querySelectorAll(".tl-item").length === 3, "the first steps");

    const detailCalls = () => calls.filter(([m, href]) => m === "GET" && href === "/agent/runs/6").length;
    await waitFor(() => detailCalls() >= 2, "the failed poll");
    await new Promise((resolve) => setImmediate(resolve)); // let the catch clear the interval
    assert.equal(detailCalls(), 2); // no further polls after the outage
    assert.equal(panel().querySelector(".badge").textContent, "RUNNING"); // last known state stays
    assert.ok(panel().querySelector(".dot-live"), "the live dot stays until a real terminal state");
  } finally { stop(); }
});

test("if the finishing fetch fails the view keeps its last known state", async () => {
  const { host, calls, badgeCalls, stop } = renderAgent({ liveRunStates: [liveDetail, liveDone, "Agent runs unavailable"] });
  try {
    await waitFor(() => settled(host), "the panels to settle");
    runRows(host)[1].click(); // the RUNNING row

    const panel = () => sectionWith(host, "Run #6");
    // The poll sees COMPLETED; finish() refetches and that fetch fails. onDone still runs.
    await waitFor(() => badgeCalls.length === 1, "onDone after the failed refetch");
    assert.equal(panel().querySelector(".badge").textContent, "COMPLETED"); // set by the poll tick before finish
    const detailCalls = () => calls.filter(([m, href]) => m === "GET" && href === "/agent/runs/6").length;
    assert.equal(detailCalls(), 3);
    assert.ok(panel().querySelectorAll(".tl-item").length >= 3, "the steps stay");
    assert.equal(panel().querySelector(".msg-text"), null); // the summary was never refilled
  } finally { stop(); }
});

test("a failed run shows its error in a danger banner", async () => {
  const { host, stop } = renderAgent({ runs: { count: 1, runs: [runFailed] } });
  try {
    await waitFor(() => settled(host), "the panels to settle");
    runRows(host)[0].click();

    const panel = () => sectionWith(host, "Run #8");
    await waitFor(() => panel()?.querySelector(".banner"), "the error banner");
    assert.equal(panel().querySelector(".badge").textContent, "FAILED");
    const banner = panel().querySelector('.banner[data-tone="danger"]');
    assert.ok(banner);
    assert.equal(banner.querySelector("div").textContent, runFailed.error);
  } finally { stop(); }
});

test("a failed resolve shows a danger toast and re-enables the row's buttons", async () => {
  const { host, posts, stop } = renderAgent({ failResolveOnce: true });
  try {
    await waitFor(() => settled(host), "the panels to settle");
    const row = approvalRows(host)[0]; // the pending one
    const reject = [...row.querySelectorAll("button")].find((b) => b.textContent === "Reject");
    const approve = [...row.querySelectorAll("button")].find((b) => b.textContent === "Approve");

    approve.click();
    await waitFor(() => document.querySelector('.toast[data-tone="danger"]'), "the danger toast");
    assert.equal(document.querySelector('.toast[data-tone="danger"]').querySelector("div").textContent, "Approval already resolved");
    assert.deepEqual(posts, [["/approvals/41/resolve", { approved: true, note: null }]]); // decide() always sends a note field
    assert.equal(reject.disabled, false); // the catch re-enables both buttons
    assert.equal(approve.disabled, false);
  } finally { stop(); }
});

test("the preview dialog shows the proposed changes as a table", async () => {
  const { host, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");
    const row = approvalRows(host)[0]; // pending with a changes preview
    [...row.querySelectorAll("button")].find((b) => b.textContent === "Preview").click();

    const dlg = document.querySelector("dialog");
    assert.equal(dlg.querySelector("#dlg-title").textContent, "update_product: proposed change");
    assert.equal(dlg.querySelector(".dialog-body p").textContent, approvalPending.summary);
    assert.deepEqual([...dlg.querySelectorAll("thead th")].map((th) => th.textContent), ["Field", "Current", "Proposed"]);
    const cells = [...dlg.querySelectorAll("tbody td")].map((td) => td.textContent);
    assert.deepEqual(cells, ["description", "Nice shoes.", "Hand-finished leather sneakers with a matte sole."]);

    dlg.querySelector(".dialog-foot .btn").click(); // Close
    await waitFor(() => dlg.open === false, "the dialog to close");
  } finally { stop(); }
});

test("a preview without a changes table falls back to raw JSON", async () => {
  const { host, stop } = renderAgent({ approvals: { count: 1, approvals: [approvalNote] } });
  try {
    await waitFor(() => settled(host), "the panels to settle");
    const row = approvalRows(host)[0];
    [...row.querySelectorAll("button")].find((b) => b.textContent === "Preview").click();

    const dlg = document.querySelector("dialog");
    assert.equal(dlg.querySelector("#dlg-title").textContent, "update_product: proposed change");
    assert.equal(dlg.querySelector(".dialog-body p").textContent, approvalNote.summary);
    assert.equal(dlg.querySelector("pre.pre").textContent, JSON.stringify(approvalNote.preview, null, 2));
  } finally { stop(); }
});

test("the tool registry dialog lists the tools; write tools are flagged for approval", async () => {
  const { host, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");
    host.querySelector(".page-head-actions .btn").click(); // Tool registry

    const dlg = document.querySelector("dialog");
    assert.equal(dlg.querySelector("#dlg-title").textContent, "Agent tool registry");
    await waitFor(() => dlg.querySelector(".dialog-body p"), "the tools to load");
    assert.equal(dlg.querySelector(".dialog-body p").textContent, "2 tools available to the agent.");
    const rows = [...dlg.querySelectorAll(".row")];
    assert.deepEqual(rows.map((r) => r.querySelector(".row-title.mono").textContent), ["list_products", "update_product"]);
    assert.deepEqual(rows.map((r) => r.querySelector(".row-sub").textContent), [toolsBody.tools[0].description, toolsBody.tools[1].description]);
    // Only the write tool needs approval.
    assert.equal(rows[0].querySelector(".badge"), null);
    assert.equal(rows[1].querySelector('.badge[data-tone="warning"]').textContent, "Approval");

    const close = dlg.querySelector(".dialog-foot .btn");
    assert.equal(close.textContent, "Close"); // the foot is filled once the body settles
  } finally { stop(); }
});

test("a failed tool registry load shows the error state in the dialog", async () => {
  const { host, stop } = renderAgent({ failToolsOnce: true });
  try {
    await waitFor(() => settled(host), "the panels to settle");
    host.querySelector(".page-head-actions .btn").click(); // Tool registry

    const dlg = document.querySelector("dialog");
    await waitFor(() => dlg.querySelector(".error-state"), "the error state");
    assert.equal(dlg.querySelector(".error-state p").textContent, "Tool registry unavailable");
  } finally { stop(); }
});

test("Ctrl+Enter submits the composer like the Run agent button", async () => {
  const { host, posts, stop } = renderAgent();
  try {
    await waitFor(() => settled(host), "the panels to settle");
    const input = host.querySelector('textarea[aria-label="Agent request"]');

    input.value = "Audit the pricing page copy";
    const enter = new Event("keydown"); // plain Enter: no modifier, nothing happens
    enter.key = "Enter";
    input.dispatchEvent(enter);
    assert.equal(posts.length, 0);

    const ctrlEnter = new Event("keydown");
    ctrlEnter.key = "Enter";
    ctrlEnter.ctrlKey = true;
    input.dispatchEvent(ctrlEnter);
    await waitFor(() => posts.length === 1, "the start-run POST");
    assert.deepEqual(posts[0], ["/agent/run", { message: "Audit the pricing page copy", session_id: "dashboard" }]);

    await waitFor(() => document.querySelector('.toast[data-tone="success"]'), "the queued toast");
    assert.equal(document.querySelector('.toast[data-tone="success"]').querySelector("div").textContent, "Run #7 queued");
    assert.equal(input.value, ""); // the composer clears on success
    await waitFor(() => sectionWith(host, "Run #7"), "the new run panel");
  } finally { stop(); }
});

