// Intelligence — market research jobs, crawled stores, scored opportunities.

import { api, settleAll, settledValue } from "../api.js";
import {
  h, fill, icon, card, metric, button, badge, statusBadge, skeleton, skeletonMetrics,
  empty, errorState, toast, dialog, num, title, meter, asyncPanel,
} from "../ui.js";

export default function intelligence(view) {
  let poll = null;

  view.append(
    h("div.page-head",
      h("div",
        h("h1", "Intelligence"),
        h("p", "Crawl competitor stores, extract products and score market opportunities."))));

  const statsHost = h("div");
  view.append(statsHost);
  const reloadStats = asyncPanel(statsHost, loadStats, renderStats, { loading: () => skeletonMetrics(4) });

  // --- new research
  const query = h("input.input", { placeholder: "e.g. minimalist ceramic homeware", "aria-label": "Research query", maxlength: 500, required: true, minlength: 3 });
  const urls = h("input.input", { placeholder: "Optional seed URLs, comma separated", "aria-label": "Seed URLs" });
  const submit = button("Start research", { variant: "primary", iconName: "search", type: "submit" });

  const form = h("form", {
    onsubmit: async (event) => {
      event.preventDefault();
      const q = query.value.trim();
      if (q.length < 3) { toast("Give the crawler at least 3 characters to work with.", "danger"); return; }
      submit.dataset.loading = "true";
      try {
        const seeds = urls.value.split(",").map((s) => s.trim()).filter(Boolean);
        const { job_id } = await api.startResearch(q, seeds);
        toast(`Research job #${job_id} queued`, "success");
        query.value = ""; urls.value = "";
        reloadJobs(); reloadStats();
        startPolling();
      } catch (err) {
        toast(err.message, "danger");
      } finally {
        delete submit.dataset.loading;
      }
    },
  },
    h("div", { style: { display: "grid", gap: "12px", gridTemplateColumns: "minmax(0,2fr) minmax(0,2fr) auto", alignItems: "end" } },
      h("label.field", h("span", "Query"), query),
      h("label.field", h("span", "Seed URLs"), urls),
      submit));

  view.append(card({ title: "New research job", sub: "Discovery → crawl → extraction → opportunity scoring" }, form));

  // --- jobs
  const jobsHost = h("section.card");
  const jobsBody = h("div.card-body.flush");
  jobsHost.append(
    h("header.card-head", h("div", h("h2", "Research jobs"), h("div.sub", "Pipeline progress per job"))),
    jobsBody);
  view.append(jobsHost);
  const reloadJobs = asyncPanel(jobsBody, () => api.researchJobs(15), renderJobs);

  // --- opportunities
  const oppsHost = h("section.card");
  const oppsBody = h("div.card-body.flush");
  const filterHost = h("div.card-head-actions");
  oppsHost.append(
    h("header.card-head",
      h("div", h("h2", "Opportunities"), h("div.sub", "Ranked by score — open one for its evidence")),
      filterHost),
    oppsBody);
  view.append(oppsHost);

  let kind = "";
  const kinds = [["", "All"], ["product_gap", "Product gaps"], ["pricing", "Pricing"], ["niche", "Niches"]];
  const tabs = h("div.tabs", { role: "tablist", "aria-label": "Opportunity type" },
    kinds.map(([value, label]) => h("button.tab", {
      type: "button", role: "tab", "aria-selected": String(value === kind),
      onclick: (event) => {
        kind = value;
        for (const tab of tabs.children) tab.setAttribute("aria-selected", "false");
        event.currentTarget.setAttribute("aria-selected", "true");
        reloadOpps();
      },
    }, label)));
  filterHost.append(tabs);
  const reloadOpps = asyncPanel(oppsBody, () => api.opportunities(kind || undefined, 60), renderOpportunities);

  // --- stores
  const storesHost = h("section.card");
  const storesBody = h("div.card-body.flush");
  storesHost.append(
    h("header.card-head", h("div", h("h2", "Crawled stores"), h("div.sub", "External sources feeding the opportunity model"))),
    storesBody);
  view.append(storesHost);
  asyncPanel(storesBody, () => api.stores(40), renderStores);

  function startPolling() {
    clearInterval(poll);
    poll = setInterval(async () => {
      if (document.hidden) return;
      try {
        const { jobs } = await api.researchJobs(15);
        reloadJobs();
        if (!(jobs || []).some((j) => ["QUEUED", "RUNNING"].includes(String(j.status).toUpperCase()))) {
          clearInterval(poll); poll = null;
          reloadOpps(); reloadStats();
        }
      } catch { clearInterval(poll); poll = null; }
    }, 5000);
  }

  api.researchJobs(15)
    .then(({ jobs }) => {
      if ((jobs || []).some((j) => ["QUEUED", "RUNNING"].includes(String(j.status).toUpperCase()))) startPolling();
    })
    .catch(() => { /* the jobs panel already surfaces this */ });

  return () => clearInterval(poll);
}

/* ----------------------------------------------------------------- stats */

async function loadStats() {
  const [jobs, opps, stores] = await settleAll([
    api.researchJobs(100), api.opportunities(undefined, 200), api.stores(200),
  ]);
  return {
    jobs: settledValue(jobs, { jobs: [] }),
    opps: settledValue(opps, { opportunities: [], count: 0 }),
    stores: settledValue(stores, { stores: [], count: 0 }),
  };
}

function renderStats({ jobs, opps, stores }) {
  const list = jobs.jobs || [];
  const running = list.filter((j) => ["RUNNING", "QUEUED"].includes(String(j.status).toUpperCase())).length;
  const opportunities = opps.opportunities || [];
  const best = opportunities.reduce((max, o) => Math.max(max, o.score || 0), 0);
  const lowComp = opportunities.filter((o) => o.competition_level === "low").length;
  const products = (stores.stores || []).reduce((sum, s) => sum + (s.product_count || 0), 0);

  return h("div.grid.grid-metrics",
    metric({ label: "Research jobs", value: num(list.length), iconName: "search",
      sub: running ? h("span", h("b", num(running)), " in flight") : "all complete" }),
    metric({ label: "Opportunities", value: num(opps.count), iconName: "target",
      sub: lowComp ? h("span", h("b", num(lowComp)), " in low-competition niches") : "no low-competition finds" }),
    metric({ label: "Best score", value: best ? best.toFixed(1) : "—", iconName: "bolt",
      tone: best >= 70 ? "success" : undefined, sub: "highest ranked opportunity" }),
    metric({ label: "Stores crawled", value: num(stores.count), iconName: "store",
      sub: h("span", h("b", num(products)), " external products") }));
}

/* ------------------------------------------------------------------ jobs */

function renderJobs(data) {
  const jobs = data.jobs || [];
  if (!jobs.length) {
    return empty({
      iconName: "search",
      title: "No research yet",
      message: "Start a job above. SPARTON discovers domains, crawls them, extracts products and scores opportunities.",
    });
  }

  const STAGES = ["discovery", "crawl", "extraction", "scoring"];
  return h("div.rows", jobs.map((job) => {
    const stats = job.stats || {};
    const stageIndex = STAGES.indexOf(String(job.stage || "").toLowerCase());
    const done = String(job.status).toUpperCase() === "COMPLETED";
    const progress = done ? 1 : stageIndex >= 0 ? (stageIndex + 1) / STAGES.length : 0.05;

    return h("div.row",
      h("div.row-main",
        h("div.row-title", `Job #${job.id}${job.stage ? ` · ${title(job.stage)}` : ""}`),
        h("div.row-sub", [
          stats.domains_discovered != null && `${num(stats.domains_discovered)} domains`,
          stats.pages_crawled != null && `${num(stats.pages_crawled)} pages`,
          stats.products_discovered != null && `${num(stats.products_discovered)} products`,
          stats.opportunities_found != null && `${num(stats.opportunities_found)} opportunities`,
        ].filter(Boolean).join(" · ") || job.error || "waiting to start"),
        h("div", { style: { marginTop: "8px", maxWidth: "320px" } },
          meter(progress, done ? "success" : String(job.status).toUpperCase() === "FAILED" ? "danger" : undefined))),
      h("div.row-side", statusBadge(job.status, ["RUNNING", "QUEUED"].includes(String(job.status).toUpperCase()))));
  }));
}

/* --------------------------------------------------------- opportunities */

function renderOpportunities(data) {
  const items = data.opportunities || [];
  if (!items.length) {
    return empty({
      iconName: "target",
      title: "No opportunities in this view",
      message: "Opportunities appear once a research job finishes scoring. Try a different type filter.",
    });
  }

  let sortKey = "score";
  let sortDir = -1;
  const body = h("tbody");

  const paint = () => {
    const sorted = [...items].sort((a, b) => {
      const av = a[sortKey] ?? 0, bv = b[sortKey] ?? 0;
      return (typeof av === "string" ? av.localeCompare(bv) : av - bv) * sortDir;
    });
    fill(body, sorted.map((opp) =>
      h("tr", { style: { cursor: "pointer" }, tabindex: "0",
        onclick: () => showOpportunity(opp.id),
        onkeydown: (e) => { if (e.key === "Enter") showOpportunity(opp.id); } },
        h("td",
          h("div.truncate", { title: opp.title }, opp.title),
          h("div.row-sub.truncate", { title: opp.summary }, opp.summary || "")),
        h("td", badge(opp.type, "primary")),
        h("td", badge(opp.competition_level || "unknown",
          opp.competition_level === "low" ? "success" : opp.competition_level === "high" ? "danger" : "warning")),
        h("td.num", `${Math.round((opp.confidence || 0) * 100)}%`),
        h("td.num", (opp.score ?? 0).toFixed(1)),
        h("td", statusBadge(opp.status)))));
  };

  const th = (label, key, numeric) => {
    const cell = h(numeric ? "th.num" : "th", { "data-sortable": "", tabindex: "0",
      "aria-sort": key === sortKey ? (sortDir === 1 ? "ascending" : "descending") : "none" },
      label, h("span.sort-caret", "▾"));
    const toggle = () => {
      sortDir = key === sortKey ? -sortDir : -1;
      sortKey = key;
      for (const other of cell.parentElement.children) other.setAttribute("aria-sort", "none");
      cell.setAttribute("aria-sort", sortDir === 1 ? "ascending" : "descending");
      paint();
    };
    cell.onclick = toggle;
    cell.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } };
    return cell;
  };

  const table = h("table",
    h("thead", h("tr",
      th("Opportunity", "title"), h("th", "Type"), h("th", "Competition"),
      th("Confidence", "confidence", true), th("Score", "score", true), h("th", "Status"))),
    body);
  paint();
  return h("div.table-wrap", table);
}

async function showOpportunity(id) {
  const el = dialog({ title: "Opportunity", body: skeleton(5), actions: [] });
  const body = el.querySelector(".dialog-body");
  try {
    const opp = await api.opportunity(id);
    const evidence = opp.evidence || {};
    fill(body,
      h("div",
        h("h3", { style: { font: "var(--t-title)", marginBottom: "6px" } }, opp.title),
        h("div.chips", badge(opp.type, "primary"), badge(`score ${(opp.score ?? 0).toFixed(1)}`, "success"),
          badge(`${Math.round((opp.confidence || 0) * 100)}% confidence`, "info"), statusBadge(opp.status))),
      h("p", { style: { color: "var(--text-2)" } }, opp.summary || "No summary."),
      opp.recommended_action
        ? h("div.banner", { "data-tone": "info" }, icon("bolt", 16), h("div", opp.recommended_action))
        : null,
      Object.keys(evidence).length
        ? h("div",
            h("div.metric-label", { style: { marginBottom: "8px" } }, "Evidence"),
            h("dl.kv", Object.entries(evidence).flatMap(([key, value]) =>
              [h("dt", title(key)), h("dd", typeof value === "object" ? JSON.stringify(value) : String(value ?? "—"))])))
        : null,
      opp.source_urls?.length
        ? h("div",
            h("div.metric-label", { style: { marginBottom: "8px" } }, "Sources"),
            h("div", { style: { display: "flex", flexDirection: "column", gap: "4px" } },
              opp.source_urls.map((url) =>
                h("a.truncate", { href: url, target: "_blank", rel: "noopener noreferrer", title: url }, url))))
        : null);
  } catch (err) {
    fill(body, errorState({ message: err.message }));
  }
  el.querySelector(".dialog-foot")?.remove();
  el.append(h("footer.dialog-foot", button("Close", { variant: "ghost", onClick: () => el.close() })));
}

/* ---------------------------------------------------------------- stores */

function renderStores(data) {
  const stores = data.stores || [];
  if (!stores.length) {
    return empty({
      iconName: "store",
      title: "No stores crawled",
      message: "Stores discovered during research show up here with their product counts.",
    });
  }
  return h("div.table-wrap",
    h("table",
      h("thead", h("tr", h("th", "Domain"), h("th", "Niche"), h("th", "Platform"), h("th.num", "Products"), h("th", "Crawl"))),
      h("tbody", stores.map((store) =>
        h("tr",
          h("td",
            h("div.truncate", { title: store.domain }, store.name || store.domain),
            store.name ? h("div.row-sub.mono", store.domain) : null),
          h("td", store.niche ? badge(store.niche, "neutral") : h("span", { style: { color: "var(--text-3)" } }, "—")),
          h("td", store.platform || "—"),
          h("td.num", num(store.product_count)),
          h("td", statusBadge(store.crawl_status)))))));
}
