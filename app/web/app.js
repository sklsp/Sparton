// SPARTON dashboard shell: session, navigation, routing, chrome.

import { api, token, onUnauthorized, ApiError } from "./api.js";
import { h, fill, icon, button, field, toast, badge } from "./ui.js";
import { ROUTES } from "./routes.js";

const root = document.getElementById("root");

export const state = {
  user: null,
  /** Cross-view counters shown in the sidebar (pending approvals). */
  pendingApprovals: 0,
};

/* -------------------------------------------------------------- theming */
const THEME_KEY = "sparton.theme";

function applyTheme(value) {
  if (value === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", value);
  localStorage.setItem(THEME_KEY, value);
}

function currentTheme() {
  return localStorage.getItem(THEME_KEY) || "system";
}

function resolvedIsDark() {
  const theme = currentTheme();
  if (theme === "dark") return true;
  if (theme === "light") return false;
  return matchMedia("(prefers-color-scheme: dark)").matches;
}

applyTheme(currentTheme());

/* ------------------------------------------------------------- routing */
const routeFromHash = () => {
  const id = (location.hash.replace(/^#\/?/, "").split("?")[0] || "overview");
  return ROUTES.find((r) => r.id === id) || ROUTES[0];
};

export const navigate = (id) => { location.hash = `#/${id}`; };

/* ---------------------------------------------------------------- login */
function renderAuth({ mode = "login" } = {}) {
  const message = h("div", { style: { display: "none" } });
  const submit = button(mode === "login" ? "Sign in" : "Create workspace", {
    variant: "primary", type: "submit", style: "width:100%",
  });

  const email = h("input.input", { type: "email", name: "email", autocomplete: "email", required: true, placeholder: "you@company.com" });
  const password = h("input.input", { type: "password", name: "password", required: true, minlength: mode === "login" ? 1 : 8, autocomplete: mode === "login" ? "current-password" : "new-password", placeholder: "••••••••" });
  const org = h("input.input", { type: "text", name: "organization_name", required: true, placeholder: "Acme Inc" });

  const form = h("form.auth-form", {
    onsubmit: async (event) => {
      event.preventDefault();
      message.style.display = "none";
      submit.dataset.loading = "true";
      try {
        const result = mode === "login"
          ? await api.login(email.value, password.value)
          : await api.register(email.value, password.value, org.value);
        token.set(result.token);
        state.user = result.user;
        toast(mode === "login" ? "Signed in" : "Workspace created", "success");
        mountShell();
      } catch (err) {
        fill(message, err.message);
        message.style.display = "";
        message.className = "banner";
        message.dataset.tone = "danger";
        password.focus();
      } finally {
        delete submit.dataset.loading;
      }
    },
  },
    field("Email", email),
    field("Password", password),
    mode === "register" ? field("Organization", org) : null,
    message,
    submit);

  fill(root, h("div.auth-shell",
    h("div.auth-card",
      h("div.auth-brand",
        h("div.brand-mark", "SP"),
        h("h1", "SPARTON"),
        h("p", mode === "login"
          ? "Knowledge, Intelligence and Create — one control plane."
          : "Create a workspace. The first account becomes its admin.")),
      h("section.card", h("div.card-body", form)),
      h("p.auth-switch",
        mode === "login" ? "No workspace yet? " : "Already have an account? ",
        h("button", {
          type: "button",
          onclick: () => renderAuth({ mode: mode === "login" ? "register" : "login" }),
        }, mode === "login" ? "Create one" : "Sign in")))));

  email.focus();
}

/* ---------------------------------------------------------------- shell */
let contentEl;
let navEl;
let crumbsEl;
let currentTeardown = null;

function navLink(route) {
  const link = h("a.nav-item", { href: `#/${route.id}`, "data-route": route.id },
    icon(route.icon, 17),
    h("span.nav-label", route.label));
  if (route.badge) link.append(h("span.nav-badge", { "data-badge": route.id, hidden: true }));
  return link;
}

function buildSidebar(app) {
  const groups = new Map();
  for (const route of ROUTES) {
    if (!groups.has(route.group)) groups.set(route.group, []);
    groups.get(route.group).push(route);
  }

  navEl = h("nav.nav", { "aria-label": "Primary" },
    [...groups].map(([name, routes]) =>
      h("div.nav-group",
        h("div.nav-group-title", name),
        routes.map(navLink))));

  const collapse = h("button.collapse-btn", {
    type: "button",
    "aria-label": "Toggle sidebar width",
    onclick: () => {
      const next = app.dataset.collapsed !== "true";
      app.dataset.collapsed = String(next);
      localStorage.setItem("sparton.collapsed", String(next));
    },
  }, icon("chevron", 16), h("span.sidebar-foot-text", "Collapse"));

  return h("aside.sidebar", { id: "sidebar" },
    h("div.brand", h("div.brand-mark", "SP"), h("span.brand-name", "SPARTON")),
    navEl,
    h("div.sidebar-foot", collapse));
}

function buildTopbar(app) {
  crumbsEl = h("div.crumbs");

  const menuBtn = h("button.icon-btn.menu-btn", {
    type: "button", "aria-label": "Open navigation", "aria-controls": "sidebar",
    "aria-expanded": "false",
    onclick: () => setMobileNav(app, app.dataset.mobileNav !== "open"),
  }, icon("menu", 18));

  const refreshBtn = h("button.icon-btn", {
    type: "button", "aria-label": "Refresh this view", title: "Refresh (R)",
    onclick: () => renderRoute(true),
  }, icon("refresh", 17));
  refreshBtnRef = refreshBtn;

  const themeBtn = h("button.icon-btn", {
    type: "button", "aria-label": "Toggle colour theme", title: "Toggle theme",
    onclick: () => {
      applyTheme(resolvedIsDark() ? "light" : "dark");
      fill(themeBtn, icon(resolvedIsDark() ? "sun" : "moon", 17));
    },
  }, icon(resolvedIsDark() ? "sun" : "moon", 17));

  const initials = (state.user?.email || "?").slice(0, 2);
  const userBtn = h("button.user-chip", {
    type: "button", "aria-label": "Account menu", onclick: openAccountMenu,
  }, h("span.avatar", initials), h("span.user-chip-name", state.user?.email || ""));

  return h("header.topbar", menuBtn, crumbsEl, h("div.topbar-spacer"),
    refreshBtn, themeBtn, userBtn);
}

let refreshBtnRef = null;

function setMobileNav(app, open) {
  app.dataset.mobileNav = open ? "open" : "closed";
  const existing = app.querySelector(".scrim");
  if (open && !existing) {
    app.append(h("div.scrim", { onclick: () => setMobileNav(app, false) }));
  } else if (!open && existing) {
    existing.remove();
  }
  app.querySelector(".menu-btn")?.setAttribute("aria-expanded", String(open));
}

/** Escape closes the mobile drawer — dialogs handle their own Escape. */
addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || document.querySelector("dialog[open]")) return;
  const app = document.querySelector(".app[data-mobile-nav='open']");
  if (app) setMobileNav(app, false);
});

function openAccountMenu() {
  import("./ui.js").then(({ dialog, button: btn }) => {
    const el = dialog({
      title: "Account",
      body: h("dl.kv",
        h("dt", "Email"), h("dd", state.user?.email || "—"),
        h("dt", "Role"), h("dd", badge(state.user?.role || "user", "primary")),
        h("dt", "Organization"), h("dd", `#${state.user?.organization_id ?? "—"}`),
        h("dt", "Theme"), h("dd", currentTheme())),
      actions: [
        btn("Close", { variant: "ghost", onClick: () => el.close() }),
        btn("Sign out", { variant: "danger", iconName: "logout", onClick: async () => {
          el.close();
          try { await api.logout(); } catch { /* token may already be dead */ }
          token.clear();
          state.user = null;
          toast("Signed out", "info");
          renderAuth();
        } }),
      ],
    });
  });
}

function mountShell() {
  const app = h("div.app", { "data-collapsed": localStorage.getItem("sparton.collapsed") || "false" });
  const sidebar = buildSidebar(app);
  contentEl = h("main.content#main", { tabindex: "-1" });
  const main = h("div.main", buildTopbar(app), contentEl);
  app.append(sidebar, main);

  fill(root,
    h("a.skip-link", { href: "#main" }, "Skip to content"),
    app);

  // Close the mobile drawer whenever navigation happens.
  navEl.addEventListener("click", (event) => {
    if (event.target.closest(".nav-item")) setMobileNav(app, false);
  });

  renderRoute();
  refreshApprovalBadge();
}

/* ------------------------------------------------------------ route host */
async function renderRoute(forced = false) {
  const route = routeFromHash();

  for (const link of navEl.querySelectorAll(".nav-item")) {
    const active = link.dataset.route === route.id;
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }

  fill(crumbsEl, h("strong", route.label));
  document.title = `${route.label} · SPARTON`;

  currentTeardown?.();
  currentTeardown = null;

  // Dialogs live on <body>, so a route change would otherwise leave one open
  // over the new view.
  for (const el of document.querySelectorAll("dialog[open]")) el.close();

  if (forced && refreshBtnRef) {
    refreshBtnRef.dataset.busy = "true";
    setTimeout(() => delete refreshBtnRef.dataset.busy, 600);
  }

  const view = h("div.view.view-enter");
  fill(contentEl, view);
  contentEl.scrollTop = 0;

  try {
    const module = await route.load();
    currentTeardown = module.default(view, { navigate, state, refreshApprovalBadge }) || null;
  } catch (err) {
    const { errorState } = await import("./ui.js");
    fill(view, errorState({
      title: "This view failed to load",
      message: err.message,
      onRetry: () => renderRoute(true),
    }));
  }
}

/** Sidebar approval counter — the one number that matters across views. */
export async function refreshApprovalBadge() {
  let count = 0;
  try {
    count = (await api.approvals()).count || 0;
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return;
  }
  state.pendingApprovals = count;
  for (const el of document.querySelectorAll("[data-badge='agent']")) {
    el.textContent = count > 99 ? "99+" : String(count);
    el.hidden = count === 0;
  }
}

/* ------------------------------------------------------------- bootstrap */
onUnauthorized.add(() => {
  if (state.user) toast("Session expired — please sign in again.", "danger");
  state.user = null;
  renderAuth();
});

addEventListener("hashchange", () => { if (state.user) renderRoute(); });

// Keyboard: `r` refreshes the current view when not typing in a field.
addEventListener("keydown", (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  const el = document.activeElement;
  if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
  if (event.key === "r" && state.user) { event.preventDefault(); renderRoute(true); }
});

async function boot() {
  if (!token.get()) return renderAuth();
  try {
    state.user = await api.me();
    mountShell();
  } catch {
    token.clear();
    renderAuth();
  }
}

boot();
