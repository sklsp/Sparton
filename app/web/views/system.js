// System — platform health, agent tools, Prometheus metrics, user admin.

import { api } from "../api.js";
import {
  h, icon, card, metric, button, badge, statusBadge, skeletonMetrics, empty, banner,
  toast, num, title, asyncPanel,
} from "../ui.js";

export default function system(view, { state }) {
  view.append(
    h("div.page-head",
      h("div",
        h("h1", "System"),
        h("p", "Platform health, the agent's tool surface and workspace administration.")),
      h("div.page-head-actions",
        button("OpenAPI docs", { iconName: "doc", onClick: () => window.open("/docs", "_blank", "noopener") }))));

  const healthHost = h("div");
  view.append(healthHost);
  const reloadHealth = asyncPanel(healthHost, () => api.health(), renderHealth, { loading: () => skeletonMetrics(4) });

  const grid = h("div.grid.grid-2");
  view.append(grid);

  // --- tools
  const toolsHost = h("section.card");
  const toolsBody = h("div.card-body.flush");
  toolsHost.append(
    h("header.card-head", h("div", h("h2", "Agent tools"), h("div.sub", "Everything the agent is allowed to call"))),
    toolsBody);
  grid.append(toolsHost);
  asyncPanel(toolsBody, () => api.tools(), renderTools);

  // --- metrics
  const metricsHost = h("section.card");
  const metricsBody = h("div.card-body.flush");
  const refresh = h("button.icon-btn", { type: "button", "aria-label": "Refresh metrics" }, icon("refresh", 15));
  metricsHost.append(
    h("header.card-head",
      h("div", h("h2", "Counters"), h("div.sub", "Live values from the Prometheus endpoint")),
      h("div.card-head-actions", refresh)),
    metricsBody);
  grid.append(metricsHost);
  const reloadMetrics = asyncPanel(metricsBody, () => api.metrics(), renderMetrics);
  refresh.onclick = () => {
    refresh.dataset.busy = "true";
    setTimeout(() => delete refresh.dataset.busy, 600);
    reloadMetrics();
  };

  // --- users (admins only — the endpoint 403s for everyone else)
  if (state.user?.role === "admin") {
    const usersHost = h("section.card");
    const usersBody = h("div.card-body.flush");
    usersHost.append(
      h("header.card-head", h("div", h("h2", "Users"), h("div.sub", "Members of this organization"))),
      usersBody);
    view.append(usersHost);
    asyncPanel(usersBody, () => api.users(), (data, reload) => renderUsers(data, reload, state));
  }

  const poll = setInterval(() => { if (!document.hidden) reloadHealth(); }, 30000);
  return () => clearInterval(poll);
}

/* ---------------------------------------------------------------- health */

function renderHealth(health) {
  const wrap = h("div");
  const degraded = health.status !== "ok";
  if (degraded) {
    wrap.append(banner("The platform is degraded: one or more dependencies are unavailable.", { tone: "warning" }));
  }

  wrap.append(h("div.grid.grid-metrics", { style: { marginTop: degraded ? "16px" : "0" } },
    metric({
      label: "Status", iconName: "system",
      value: title(health.status), tone: health.status === "ok" ? "success" : "warning",
      sub: "aggregate platform health",
    }),
    metric({
      label: "Database", iconName: "db",
      value: health.database?.ok ? "Connected" : "Down",
      tone: health.database?.ok ? "success" : "danger",
      sub: `${health.database?.url_scheme || "unknown"} backend`,
    }),
    metric({
      label: "LLM provider", iconName: "cpu",
      value: health.llm?.available ? "Available" : "Offline",
      tone: health.llm?.available ? "success" : "danger",
      sub: `${health.llm?.provider || "unknown"}${health.llm?.model ? ` · ${health.llm.model}` : ""}`,
    }),
    metric({ label: "Agent tools", iconName: "agent", value: num(health.agent_tools), sub: "registered and callable" })));
  return wrap;
}

/* ----------------------------------------------------------------- tools */

function renderTools(data) {
  const tools = data.tools || [];
  if (!tools.length) return empty({ iconName: "agent", title: "No tools registered" });

  const byCategory = new Map();
  for (const tool of tools) {
    const key = tool.category || "general";
    if (!byCategory.has(key)) byCategory.set(key, []);
    byCategory.get(key).push(tool);
  }

  return h("div",
    [...byCategory].map(([category, items]) =>
      h("div",
        h("div", { style: { padding: "12px 20px 4px", font: "var(--t-label)", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--text-2)", background: "var(--sunken)" } },
          `${category} · ${items.length}`),
        h("div.rows", items.map((tool) =>
          h("div.row",
            h("div.row-main",
              h("div.row-title.mono", tool.name),
              h("div.row-sub", tool.description || "No description")),
            h("div.row-side",
              tool.access ? badge(tool.access, tool.access === "write" ? "warning" : "neutral") : null,
              tool.requires_approval ? badge("approval", "danger") : null)))))));
}

/* --------------------------------------------------------------- metrics */

/** Parse the Prometheus text exposition into name/value rows. */
function renderMetrics(text) {
  const rows = [];
  for (const line of String(text || "").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const match = /^([a-zA-Z_:][\w:]*(?:\{[^}]*\})?)\s+([0-9eE+\-.]+)$/.exec(trimmed);
    if (match) rows.push([match[1], Number(match[2])]);
  }
  if (!rows.length) {
    return empty({ iconName: "chart", title: "No counters yet", message: "Metrics appear once requests, runs or jobs have been recorded." });
  }
  return h("div.table-wrap",
    h("table",
      h("thead", h("tr", h("th", "Metric"), h("th.num", "Value"))),
      h("tbody", rows.map(([name, value]) =>
        h("tr", h("td.mono.truncate", { title: name }, name), h("td.num", num(value)))))));
}

/* ----------------------------------------------------------------- users */

function renderUsers(data, reload, state) {
  const users = data.users || [];
  if (!users.length) return empty({ iconName: "users", title: "No users found" });

  return h("div.table-wrap",
    h("table",
      h("thead", h("tr", h("th", "Email"), h("th", "Role"), h("th", "Status"), h("th", ""))),
      h("tbody", users.map((user) => {
        const isSelf = user.id === state.user?.id;
        const toggle = async (event) => {
          const btn = event.currentTarget;
          btn.dataset.loading = "true";
          try {
            await api.patchUser(user.id, { is_active: !user.is_active });
            toast(user.is_active ? "User disabled" : "User enabled", "success");
            reload();
          } catch (err) {
            toast(err.message, "danger");
            delete btn.dataset.loading;
          }
        };
        return h("tr",
          h("td", user.email, isSelf ? h("span.chip", { style: { marginLeft: "8px" } }, "you") : null),
          h("td", badge(user.role, user.role === "admin" ? "primary" : "neutral")),
          h("td", statusBadge(user.is_active ? "active" : "disabled")),
          h("td", { style: { width: "1%" } },
            isSelf
              ? null
              : button(user.is_active ? "Disable" : "Enable", {
                  size: "sm", variant: user.is_active ? "danger" : "primary", onClick: toggle,
                })));
      }))));
}
