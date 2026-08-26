// Overview — the whole platform in one screen.

import { api, settleAll, settledValue } from "../api.js";
import {
  h, fill, icon, card, metric, button, statusBadge, badge, skeletonMetrics, empty,
  banner, toast, num, ago, tone, title, meter, asyncPanel,
} from "../ui.js";

export default function overview(view, { navigate, state, refreshApprovalBadge }) {
  const timers = [];

  view.append(
    h("div.page-head",
      h("div",
        h("h1", greeting(state.user)),
        h("p", "Live state of the SPARTON platform — agent activity, knowledge base, market intelligence and creation.")),
      h("div.page-head-actions",
        button("Ask the agent", { variant: "primary", iconName: "agent", onClick: () => navigate("agent") }),
        button("Open API docs", { iconName: "doc", onClick: () => window.open("/docs", "_blank", "noopener") }))));

  // --- health banner: only shown when something is actually wrong.
  const healthHost = h("div");
  view.append(healthHost);
  api.health().then((health) => {
    const problems = [];
    if (!health.database?.ok) problems.push("the database is unreachable");
    if (!health.llm?.available) problems.push(`the ${health.llm?.provider || "LLM"} provider is offline`);
    if (!problems.length) return;
    fill(healthHost, banner(
      `Degraded: ${problems.join(" and ")}. Agent and chat features will fail until this is resolved.`,
      { tone: "warning", action: button("System", { size: "sm", onClick: () => navigate("system") }) }));
  }).catch(() => {
    fill(healthHost, banner("Could not read platform health.", { tone: "danger" }));
  });

  // --- metrics
  const metricsHost = h("div");
  view.append(metricsHost);
  const reloadMetrics = asyncPanel(metricsHost, loadMetrics, renderMetrics, {
    loading: () => skeletonMetrics(4),
  });

  // --- main grid: activity + agent status
  const activityHost = h("section.card");
  const agentHost = h("section.card");
  view.append(h("div.grid.grid-2", activityHost, agentHost));

  const reloadActivity = panelOf(activityHost, "Recent activity", "Agent runs, research jobs and generations",
    loadActivity, renderActivity, navigate);
  const reloadAgents = panelOf(agentHost, "Approvals & agent state", "Writes waiting on a human decision",
    loadAgentState, (data, reload) => renderAgentState(data, reload, { navigate, refreshApprovalBadge }), navigate);

  // --- knowledge + quick actions
  const knowledgeHost = h("section.card");
  const actionsHost = card({ title: "Quick actions" }, quickActions(navigate));
  view.append(h("div.grid.grid-2", knowledgeHost, actionsHost));
  const reloadKnowledge = panelOf(knowledgeHost, "Knowledge base", "Documents indexed for retrieval",
    loadKnowledge, renderKnowledge, navigate);

  // --- opportunities
  const oppsHost = h("section.card");
  view.append(oppsHost);
  const reloadOpps = panelOf(oppsHost, "Top opportunities", "Highest scoring findings from market research",
    () => api.opportunities(undefined, 6), renderOpportunities, navigate);

  // Poll while anything is in flight; stop as soon as the page is hidden.
  const poll = setInterval(() => {
    if (document.hidden) return;
    reloadMetrics();
    reloadActivity();
    reloadAgents();
  }, 15000);
  timers.push(poll);

  return () => timers.forEach(clearInterval);
}

/* --------------------------------------------------------------- helpers */

function greeting(user) {
  const hour = new Date().getHours();
  const part = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const name = (user?.email || "").split("@")[0];
  return name ? `${part}, ${name}` : part;
}

/** Card-with-async-body: one consistent loading/empty/error contract. */
function panelOf(host, heading, sub, load, render, navigate) {
  const body = h("div.card-body.flush");
  const refresh = h("button.icon-btn", { type: "button", "aria-label": `Refresh ${heading}` }, icon("refresh", 15));
  host.append(
    h("header.card-head",
      h("div", h("h2", heading), h("div.sub", sub)),
      h("div.card-head-actions", refresh)),
    body);
  const reload = asyncPanel(body, load, (data, r) => render(data, r, navigate));
  refresh.onclick = () => {
    refresh.dataset.busy = "true";
    setTimeout(() => delete refresh.dataset.busy, 600);
    reload();
  };
  return reload;
}

/* --------------------------------------------------------------- metrics */

async function loadMetrics() {
  // Independent endpoints — one failure must not blank the whole strip.
  const [runs, approvals, docs, rag, opportunities, analytics, jobs] = await settleAll([
    api.runs(50), api.approvals(), api.documents(200), api.ragStatus(),
    api.opportunities(undefined, 200), api.analytics(), api.researchJobs(50),
  ]);
  return {
    runs: settledValue(runs, { runs: [], count: 0 }),
    approvals: settledValue(approvals, { count: 0, approvals: [] }),
    docs: settledValue(docs, { count: 0, documents: [] }),
    rag: settledValue(rag, null),
    opportunities: settledValue(opportunities, { count: 0, opportunities: [] }),
    analytics: settledValue(analytics, null),
    jobs: settledValue(jobs, { jobs: [], count: 0 }),
  };
}

function renderMetrics(data) {
  const runs = data.runs.runs || [];
  const active = runs.filter((r) => ["RUNNING", "PENDING", "QUEUED"].includes(String(r.status).toUpperCase())).length;
  const failed = runs.filter((r) => String(r.status).toUpperCase() === "FAILED").length;
  const jobs = data.jobs.jobs || [];
  const jobsActive = jobs.filter((j) => ["RUNNING", "QUEUED"].includes(String(j.status).toUpperCase())).length;
  const lowCompetition = (data.opportunities.opportunities || [])
    .filter((o) => o.competition_level === "low").length;

  return h("div.grid.grid-metrics",
    metric({
      label: "Agent runs", iconName: "agent", value: num(runs.length),
      sub: active
        ? h("span", h("b", num(active)), " running now")
        : failed ? h("span", h("b", num(failed)), " failed recently") : "all idle",
    }),
    metric({
      label: "Pending approvals", iconName: "approvals",
      value: num(data.approvals.count), tone: data.approvals.count ? "warning" : undefined,
      sub: data.approvals.count ? "waiting on a human decision" : "nothing blocked",
    }),
    metric({
      label: "Knowledge chunks", iconName: "knowledge",
      value: num(data.rag?.chunks ?? 0),
      sub: h("span", "across ", h("b", num(data.docs.count)), " documents"),
    }),
    metric({
      label: "Opportunities", iconName: "target", value: num(data.opportunities.count),
      sub: jobsActive
        ? h("span", h("b", num(jobsActive)), " research jobs running")
        : lowCompetition
          ? h("span", h("b", num(lowCompetition)), " in low-competition niches")
          : data.opportunities.count ? "all in contested niches" : "no research yet",
    }));
}

/* -------------------------------------------------------------- activity */

async function loadActivity() {
  const [runs, jobs, images] = await settleAll([
    api.runs(8), api.researchJobs(8), api.generated(8),
  ]);
  const items = [];
  if (runs.status === "fulfilled") {
    for (const run of runs.value.runs || []) {
      items.push({
        kind: "Agent run", tone: tone(run.status), status: run.status,
        title: run.request || `Run #${run.id}`,
        at: run.completed_at || run.started_at,
        meta: `${run.tool_calls_made ?? 0} tool calls · ${run.iterations ?? 0} iterations`,
        route: "agent",
      });
    }
  }
  if (jobs.status === "fulfilled") {
    for (const job of jobs.value.jobs || []) {
      const stats = job.stats || {};
      items.push({
        kind: "Research", tone: tone(job.status), status: job.status,
        title: job.stage ? `${title(job.stage)} · job #${job.id}` : `Research job #${job.id}`,
        meta: [
          stats.domains_discovered != null && `${stats.domains_discovered} domains`,
          stats.pages_crawled != null && `${stats.pages_crawled} pages`,
          stats.opportunities_found != null && `${stats.opportunities_found} opportunities`,
        ].filter(Boolean).join(" · ") || job.error || "queued",
        route: "intelligence",
      });
    }
  }
  if (images.status === "fulfilled") {
    for (const image of (images.value.images || []).slice(0, 4)) {
      items.push({
        kind: "Generation", tone: "success", status: "completed",
        title: image.filename, at: image.created_at || image.modified,
        meta: "image generated", route: "create",
      });
    }
  }
  items.sort((a, b) => new Date(b.at || 0) - new Date(a.at || 0));
  return items.slice(0, 12);
}

function renderActivity(items, _reload, navigate) {
  if (!items.length) {
    return empty({
      iconName: "clock",
      title: "No activity yet",
      message: "Agent runs, research jobs and generations will appear here as soon as work starts.",
      action: button("Start an agent run", { size: "sm", variant: "primary", onClick: () => navigate("agent") }),
    });
  }
  return h("div.timeline",
    items.map((item) =>
      h("div.tl-item", { "data-tone": item.tone },
        h("div.tl-rail", h("span.tl-node"), h("span.tl-line")),
        h("div.tl-body",
          h("div.tl-title.truncate", { title: item.title }, item.title),
          h("div.tl-meta",
            h("span", item.kind),
            h("span", "·"),
            statusBadge(item.status),
            h("span", item.meta),
            item.at ? h("span", `· ${ago(item.at)}`) : null)))));
}

/* ------------------------------------------------------------ agent state */

async function loadAgentState() {
  const [approvals, runs] = await Promise.all([api.approvals(), api.runs(5)]);
  return { approvals: approvals.approvals || [], runs: runs.runs || [] };
}

function renderAgentState({ approvals, runs }, reload, { navigate, refreshApprovalBadge }) {
  const wrap = h("div");

  if (approvals.length) {
    wrap.append(h("div.rows",
      approvals.slice(0, 4).map((approval) => {
        const decide = async (approved) => {
          row.querySelectorAll("button").forEach((b) => { b.disabled = true; });
          try {
            await api.resolveApproval(approval.id, approved, null);
            toast(approved ? `Approved ${approval.tool}` : `Rejected ${approval.tool}`, approved ? "success" : "info");
            refreshApprovalBadge();
            reload();
          } catch (err) {
            toast(err.message, "danger");
            row.querySelectorAll("button").forEach((b) => { b.disabled = false; });
          }
        };
        const row = h("div.row",
          h("div.row-main",
            h("div.row-title.truncate", { title: approval.summary }, approval.summary || approval.tool),
            h("div.row-sub", `${approval.tool} · run #${approval.run_id} · ${ago(approval.created_at)}`)),
          h("div.row-side",
            button("Reject", { size: "sm", onClick: () => decide(false) }),
            button("Approve", { size: "sm", variant: "primary", onClick: () => decide(true) })));
        return row;
      })));
  } else if (runs.length) {
    wrap.append(h("div.rows",
      runs.map((run) => h("div.row",
        h("div.row-main",
          h("div.row-title.truncate", { title: run.request }, run.request || `Run #${run.id}`),
          h("div.row-sub", `${ago(run.started_at)} · ${run.tool_calls_made ?? 0} tool calls`)),
        h("div.row-side", statusBadge(run.status, ["RUNNING", "PENDING"].includes(String(run.status).toUpperCase())))))));
  } else {
    wrap.append(empty({
      iconName: "approvals",
      title: "Nothing waiting on you",
      message: "When the agent proposes a write it will pause here for approval.",
      action: button("Open agent", { size: "sm", onClick: () => navigate("agent") }),
    }));
  }

  if (approvals.length > 4 || runs.length) {
    wrap.append(h("footer.card-foot",
      h("span", approvals.length ? `${approvals.length} pending approvals` : `${runs.length} recent runs`),
      h("div", { style: { marginLeft: "auto" } },
        button("View all", { size: "sm", variant: "ghost", onClick: () => navigate("agent") }))));
  }
  return wrap;
}

/* ------------------------------------------------------------- knowledge */

async function loadKnowledge() {
  const [docs, rag] = await Promise.all([api.documents(6), api.ragStatus().catch(() => null)]);
  return { docs: docs.documents || [], total: docs.count || 0, rag };
}

function renderKnowledge({ docs, rag }, _reload, navigate) {
  if (!docs.length) {
    return empty({
      iconName: "knowledge",
      title: "No documents indexed",
      message: "Upload PDFs, Word files or text and the agent can cite them in every answer.",
      action: button("Go to Knowledge", { size: "sm", variant: "primary", onClick: () => navigate("knowledge") }),
    });
  }
  return h("div",
    rag ? h("div", { style: { padding: "16px 20px 0" } },
      h("div.metric-sub", { style: { marginBottom: "8px" } },
        h("b", num(rag.chunks)), " chunks · ", rag.embedding_backend || "unknown", " · ", rag.embedding_model || "—")) : null,
    h("div.rows",
      docs.map((doc) => h("div.row",
        h("span.action-icon", icon("doc", 15)),
        h("div.row-main",
          h("div.row-title.truncate", { title: doc.title }, doc.title),
          h("div.row-sub", `${num(doc.chunks)} chunks · ${ago(doc.created_at)}`)),
        h("div.row-side", badge(`${num(doc.chunks)} ch`, "neutral"))))));
}

/* ---------------------------------------------------------- opportunities */

function renderOpportunities(data, _reload, navigate) {
  const items = data.opportunities || [];
  if (!items.length) {
    return empty({
      iconName: "target",
      title: "No opportunities found yet",
      message: "Run market research and scored opportunities will be ranked here.",
      action: button("Start research", { size: "sm", variant: "primary", onClick: () => navigate("intelligence") }),
    });
  }
  return h("div.table-wrap",
    h("table",
      h("thead", h("tr",
        h("th", "Opportunity"), h("th", "Type"), h("th", "Competition"),
        h("th.num", "Confidence"), h("th.num", "Score"))),
      h("tbody",
        [...items].sort((a, b) => b.score - a.score).slice(0, 6).map((opp) =>
          h("tr",
            h("td",
              h("div.truncate", { title: opp.title }, opp.title),
              h("div.row-sub.truncate", { title: opp.summary }, opp.summary || "")),
            h("td", badge(opp.type, "primary")),
            h("td", badge(opp.competition_level || "unknown",
              opp.competition_level === "low" ? "success" : opp.competition_level === "high" ? "danger" : "warning")),
            h("td.num", `${Math.round((opp.confidence || 0) * 100)}%`),
            h("td.num", { style: { minWidth: "110px" } },
              h("div", { style: { display: "flex", alignItems: "center", gap: "8px", justifyContent: "flex-end" } },
                h("span", (opp.score ?? 0).toFixed(1)),
                h("div", { style: { width: "56px" } }, meter((opp.score ?? 0) / 100, "success")))))))));
}

/* ------------------------------------------------------------- shortcuts */

function quickActions(navigate) {
  const items = [
    { icon: "agent", title: "Run the agent", desc: "Give Athena a task with tool access", to: "agent" },
    { icon: "upload", title: "Upload documents", desc: "Index files for retrieval-augmented chat", to: "knowledge" },
    { icon: "search", title: "Research a market", desc: "Crawl stores and score opportunities", to: "intelligence" },
    { icon: "image", title: "Generate an image", desc: "Queue a ComfyUI workflow", to: "create" },
  ];
  return h("div.actions",
    items.map((item) =>
      h("button.action", { type: "button", onclick: () => navigate(item.to) },
        h("span.action-icon", icon(item.icon, 16)),
        h("span.action-text",
          h("span.action-title", item.title),
          h("span.action-desc", item.desc)),
        h("span.action-chev", icon("chevron", 15)))));
}
