// Agent — start runs, watch the live step feed, resolve approvals.

import { api } from "../api.js";
import {
  h, fill, icon, card, button, statusBadge, badge, skeleton, empty, errorState,
  toast, dialog, confirmDialog, num, ago, when, tone, title, asyncPanel, scrollBehavior,
} from "../ui.js";

export default function agentView(view, { refreshApprovalBadge }) {
  let runPoll = null;      // follows one in-flight run
  let approvalPoll = null; // keeps the approvals panel fresh
  const stopRunPoll = () => { clearInterval(runPoll); runPoll = null; };
  const stop = () => { stopRunPoll(); clearInterval(approvalPoll); approvalPoll = null; };

  view.append(
    h("div.page-head",
      h("div",
        h("h1", "Agent"),
        h("p", "Athena runs with tool access. Every write pauses for your approval before it lands.")),
      h("div.page-head-actions",
        button("Tool registry", { iconName: "system", onClick: showTools }))));

  // --- composer
  const input = h("textarea.textarea", {
    placeholder: "e.g. Find products with weak descriptions and propose better copy",
    rows: 3, "aria-label": "Agent request", maxlength: 8000,
  });
  const submit = button("Run agent", { variant: "primary", iconName: "send", type: "submit" });

  const composer = h("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      const message = input.value.trim();
      if (!message) return;
      submit.dataset.loading = "true";
      try {
        const { run_id } = await api.startRun(message);
        input.value = "";
        toast(`Run #${run_id} queued`, "success");
        openRun(run_id);
        reloadRuns();
      } catch (err) {
        toast(err.message, "danger");
      } finally {
        delete submit.dataset.loading;
      }
    },
  },
    h("div", { style: { display: "flex", flexDirection: "column", gap: "12px" } },
      input,
      h("div", { style: { display: "flex", gap: "8px", alignItems: "center", flexWrap: "wrap" } },
        h("span.metric-sub", { style: { marginRight: "auto" } },
          "Runs execute on a background worker. ",
          h("kbd.chip", "Ctrl"), " + ", h("kbd.chip", "Enter"), " to submit."),
        submit)));

  input.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") composer.requestSubmit();
  });

  view.append(card({ title: "New run", sub: "Describe the outcome, not the steps" }, composer));

  // --- approvals
  const approvalsHost = h("section.card");
  const approvalsBody = h("div.card-body.flush");
  approvalsHost.append(
    h("header.card-head",
      h("div", h("h2", "Approvals"), h("div.sub", "Proposed writes waiting on a decision")),
      h("div.card-head-actions", h("div", { id: "approval-filter" }))),
    approvalsBody);
  view.append(approvalsHost);

  let approvalFilter = "";
  const filterTabs = h("div.tabs", { role: "tablist", "aria-label": "Approval status" },
    [["", "Pending"], ["APPROVED", "Approved"], ["REJECTED", "Rejected"]].map(([value, label]) =>
      h("button.tab", {
        type: "button", role: "tab", "aria-selected": String(value === approvalFilter),
        onclick: (event) => {
          approvalFilter = value;
          for (const tab of filterTabs.children) tab.setAttribute("aria-selected", "false");
          event.currentTarget.setAttribute("aria-selected", "true");
          reloadApprovals();
        },
      }, label)));
  approvalsHost.querySelector("#approval-filter").replaceWith(filterTabs);

  const reloadApprovals = asyncPanel(
    approvalsBody,
    () => api.approvals(approvalFilter || undefined),
    (data, reload) => renderApprovals(data, reload, refreshApprovalBadge, approvalFilter));

  // --- runs
  const runsHost = h("section.card");
  const runsBody = h("div.card-body.flush");
  runsHost.append(
    h("header.card-head",
      h("div", h("h2", "Runs"), h("div.sub", "Most recent first — select a run for its full step trace"))),
    runsBody);
  view.append(runsHost);

  const reloadRuns = asyncPanel(runsBody, () => api.runs(25), (data) => renderRuns(data, openRun));

  // --- live run detail
  const detailHost = h("div");
  view.append(detailHost);

  function openRun(runId) {
    stopRunPoll();
    fill(detailHost, card({ title: `Run #${runId}`, sub: "Loading trace…" }, skeleton(3)));
    detailHost.scrollIntoView({ block: "nearest", behavior: scrollBehavior() });

    const stepsHost = h("div.timeline");
    const statusHost = h("div.card-head-actions");
    const panel = h("section.card",
      h("header.card-head",
        h("div", h("h2", `Run #${runId}`), h("div.sub", "Live step feed")),
        statusHost),
      h("div.card-body.flush", stepsHost));
    const summaryHost = h("div");
    panel.append(summaryHost);

    const seen = new Set();
    const addStep = (step) => {
      if (seen.has(step.step_number)) return;
      seen.add(step.step_number);
      const emptyEl = stepsHost.querySelector(".empty");
      emptyEl?.remove();
      stepsHost.append(stepRow(step));
      stepsHost.lastElementChild.scrollIntoView({ block: "nearest" });
    };

    api.run(runId).then((run) => {
      fill(detailHost, panel);
      fill(statusHost, statusBadge(run.status, ["RUNNING", "PENDING"].includes(String(run.status).toUpperCase())));
      if (!run.steps?.length) {
        fill(stepsHost, empty({ iconName: "clock", title: "Waiting for the first step", message: "The worker has not reported progress yet." }));
      } else {
        run.steps.forEach(addStep);
      }
      renderSummary(summaryHost, run);
      if (!["COMPLETED", "FAILED", "CANCELLED"].includes(String(run.status).toUpperCase())) {
        subscribe(runId, addStep, statusHost, summaryHost, () => { reloadRuns(); reloadApprovals(); refreshApprovalBadge(); });
      }
    }).catch((err) => {
      fill(detailHost, card({ title: `Run #${runId}` },
        errorState({ message: err.message, onRetry: () => openRun(runId) })));
    });
  }

  /** The /events SSE endpoint is header-authenticated and EventSource cannot
   *  send an Authorization header — a token in the URL would end up in access
   *  logs. Polling the run gives the same feed at a 2s cadence. */
  function subscribe(runId, addStep, statusHost, summaryHost, onDone) {
    const finish = async () => {
      stopRunPoll();
      try {
        const run = await api.run(runId);
        fill(statusHost, statusBadge(run.status));
        renderSummary(summaryHost, run);
      } catch { /* view already shows the last known state */ }
      onDone();
    };

    stopRunPoll();
    runPoll = setInterval(async () => {
      if (document.hidden) return;
      try {
        const run = await api.run(runId);
        run.steps?.forEach(addStep);
        const status = String(run.status).toUpperCase();
        fill(statusHost, statusBadge(run.status, ["RUNNING", "PENDING"].includes(status)));
        if (["COMPLETED", "FAILED", "CANCELLED"].includes(status)) finish();
      } catch { stopRunPoll(); }
    }, 2000);
  }

  approvalPoll = setInterval(() => { if (!document.hidden) reloadApprovals(); }, 20000);
  return stop;
}

/* ------------------------------------------------------------- fragments */

function stepRow(step) {
  const status = String(step.status || "").toUpperCase();
  return h("div.tl-item", { "data-tone": tone(status) },
    h("div.tl-rail", h("span.tl-node"), h("span.tl-line")),
    h("div.tl-body",
      h("div.tl-title", step.message || title(step.type)),
      h("div.tl-meta",
        h("span", `#${step.step_number}`),
        badge(step.type || "step", "neutral"),
        step.tool ? h("span.chip", step.tool) : null,
        step.status ? statusBadge(step.status) : null)));
}

function renderSummary(host, run) {
  const parts = [];
  if (run.final_response) {
    parts.push(h("div.card-body",
      h("div.metric-label", { style: { marginBottom: "8px" } }, "Final response"),
      h("div.msg-text", run.final_response)));
  }
  if (run.error) {
    parts.push(h("div.card-body", h("div.banner", { "data-tone": "danger" }, icon("alert", 16), h("div", run.error))));
  }
  parts.push(h("footer.card-foot",
    h("span", `${num(run.iterations ?? 0)} iterations`),
    h("span", "·"),
    h("span", `${num(run.tool_calls_made ?? 0)} tool calls`),
    h("span", "·"),
    h("span", `started ${when(run.started_at)}`)));
  fill(host, parts);
}

function renderApprovals(data, reload, refreshApprovalBadge, filter) {
  const items = data.approvals || [];
  if (!items.length) {
    return empty({
      iconName: "approvals",
      title: filter ? `No ${filter.toLowerCase()} approvals` : "Nothing waiting on you",
      message: filter
        ? "Nothing has been resolved with this status yet."
        : "When the agent proposes a write it pauses here until you approve or reject it.",
    });
  }

  return h("div.rows", items.map((approval) => {
    const pending = String(approval.status).toUpperCase() === "PENDING";

    const decide = async (approved) => {
      if (!approved) {
        const ok = await confirmDialog({
          title: "Reject this write?",
          message: `${approval.tool} will be skipped and the run will continue without it.`,
          confirmLabel: "Reject", variant: "danger",
        });
        if (!ok) return;
      }
      row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
      try {
        await api.resolveApproval(approval.id, approved, null);
        toast(approved ? "Approved — the run is resuming" : "Rejected", approved ? "success" : "info");
        refreshApprovalBadge();
        reload();
      } catch (err) {
        toast(err.message, "danger");
        row.querySelectorAll("button").forEach((b) => { b.disabled = false; });
      }
    };

    const row = h("div.row",
      h("div.row-main",
        h("div.row-title", approval.summary || approval.tool),
        h("div.row-sub", `${approval.tool} · run #${approval.run_id} · ${ago(approval.created_at)}`)),
      h("div.row-side",
        approval.preview ? button("Preview", { size: "sm", variant: "ghost", onClick: () => showPreview(approval) }) : null,
        pending
          ? [button("Reject", { size: "sm", onClick: () => decide(false) }),
             button("Approve", { size: "sm", variant: "primary", onClick: () => decide(true) })]
          : statusBadge(approval.status)));
    return row;
  }));
}

function showPreview(approval) {
  const preview = approval.preview || {};
  const changes = Array.isArray(preview.changes) ? preview.changes : null;
  const el = dialog({
    title: `${approval.tool} — proposed change`,
    body: [
      h("p", { style: { color: "var(--text-2)" } }, approval.summary || "No summary provided."),
      changes
        ? h("div.table-wrap", h("table",
            h("thead", h("tr", h("th", "Field"), h("th", "Current"), h("th", "Proposed"))),
            h("tbody", changes.map((change) => h("tr",
              h("td.mono", change.field),
              h("td", { style: { color: "var(--text-3)" } }, String(change.current ?? "—")),
              h("td", { style: { color: "var(--success)" } }, String(change.proposed ?? "—")))))))
        : h("pre.pre", JSON.stringify(preview, null, 2)),
    ],
    actions: [button("Close", { variant: "ghost", onClick: () => el.close() })],
  });
}

function renderRuns(data, openRun) {
  const runs = data.runs || [];
  if (!runs.length) {
    return empty({
      iconName: "agent",
      title: "No runs yet",
      message: "Describe a task above and the agent will plan, call tools and report back.",
    });
  }
  return h("div.table-wrap",
    h("table",
      h("thead", h("tr",
        h("th", "Request"), h("th", "Status"), h("th.num", "Steps"),
        h("th", "Started"), h("th", ""))),
      h("tbody", runs.map((run) =>
        h("tr", { style: { cursor: "pointer" }, onclick: () => openRun(run.id), tabindex: "0",
          onkeydown: (e) => { if (e.key === "Enter") openRun(run.id); } },
          h("td", h("div.truncate", { title: run.request }, run.request || `Run #${run.id}`)),
          h("td", statusBadge(run.status, ["RUNNING", "PENDING"].includes(String(run.status).toUpperCase()))),
          h("td.num", num(run.tool_calls_made ?? 0)),
          h("td", { style: { color: "var(--text-3)" } }, ago(run.started_at)),
          h("td", { style: { width: "1%" } }, h("span", { style: { color: "var(--text-3)" } }, icon("chevron", 15))))))));
}

async function showTools() {
  const el = dialog({
    title: "Agent tool registry",
    body: h("div", skeleton(4)),
    actions: [],
  });
  const body = el.querySelector(".dialog-body");
  try {
    const { tools, count } = await api.tools();
    fill(body,
      h("p", { style: { color: "var(--text-2)" } }, `${count} tools available to the agent.`),
      h("div.rows", { style: { border: "1px solid var(--border)", borderRadius: "var(--r-md)" } },
        tools.map((tool) => h("div.row",
          h("div.row-main",
            h("div.row-title.mono", tool.name),
            h("div.row-sub", tool.description || "")),
          tool.requires_approval ? h("div.row-side", badge("approval", "warning")) : null))));
  } catch (err) {
    fill(body, errorState({ message: err.message }));
  }
  el.querySelector(".dialog-foot")?.replaceChildren(button("Close", { variant: "ghost", onClick: () => el.close() }));
}
