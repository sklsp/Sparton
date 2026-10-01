// Sparton dashboard shell: session, the board header, navigation, routing.

import { api, token, onUnauthorized } from "./api.js";
import { h, fill, button, field, toast } from "./ui.js";
import { ROUTES } from "./routes.js";
import { t, langSwitch } from "./i18n.js";
import { flapWord } from "./board.js";

const root = document.getElementById("root");

export const state = { user: null };

/* ------------------------------------------------------------- routing */
const routeFromHash = () => {
  const id = location.hash.replace(/^#\/?/, "").split("?")[0] || "overview";
  return ROUTES.find((r) => r.id === id) || ROUTES[0];
};

export const navigate = (id) => { location.hash = `#/${id}`; };

/* ---------------------------------------------------------------- login */
function renderAuth({ mode = "login" } = {}) {
  const message = h("p.form-error", { role: "alert", hidden: true });
  const submit = button(t(mode === "login" ? "auth.signin" : "auth.create"), { variant: "primary", type: "submit", size: "lg" });

  const email = h("input.input", { type: "email", name: "email", autocomplete: "email", required: true });
  const password = h("input.input", { type: "password", name: "password", required: true, minlength: mode === "login" ? 1 : 8, autocomplete: mode === "login" ? "current-password" : "new-password" });
  const org = h("input.input", { type: "text", name: "organization_name", required: true, autocomplete: "organization" });

  const form = h("form.auth-form", {
    onsubmit: async (event) => {
      event.preventDefault();
      message.hidden = true;
      submit.dataset.loading = "true";
      try {
        const result = mode === "login"
          ? await api.login(email.value, password.value)
          : await api.register(email.value, password.value, org.value);
        token.set(result.token);
        state.user = result.user;
        mountShell();
      } catch (err) {
        fill(message, err.message);
        message.hidden = false;
        password.focus();
      } finally {
        delete submit.dataset.loading;
      }
    },
  },
  field(t("auth.email"), email),
  field(t("auth.password"), password),
  mode === "register" ? field(t("auth.shopName"), org) : null,
  message,
  submit);

  fill(root, h("div.auth-shell",
    h("header.auth-top", h("a.brand", { href: "/", "aria-label": t("brand.home") }, flapWord("SPARTON")), langSwitch()),
    h("main.auth-card#main",
      h("h1", t(mode === "login" ? "auth.loginTitle" : "auth.signupTitle")),
      h("p.auth-lede", t(mode === "login" ? "auth.loginLede" : "auth.signupLede")),
      form,
      h("p.auth-switch",
        t(mode === "login" ? "auth.noAccount" : "auth.haveAccount"), " ",
        h("button", { type: "button", onclick: () => renderAuth({ mode: mode === "login" ? "register" : "login" }) },
          t(mode === "login" ? "auth.toSignup" : "auth.toLogin"))))));
  email.focus();
}

/* ---------------------------------------------------------------- shell */
let contentEl;
let navEl;
let currentTeardown = null;

function buildHeader(app) {
  navEl = h("nav.nav", { "aria-label": t("nav.label") },
    ROUTES.map((r) => h("a.nav-item", { href: `#/${r.id}`, "data-route": r.id }, t(`nav.${r.id}`))));

  const menuBtn = h("button.menu-btn", {
    type: "button", "aria-controls": "nav-drawer", "aria-expanded": "false",
    onclick: () => setMobileNav(app, app.dataset.nav !== "open"),
  }, h("span.menu-bars", { "aria-hidden": "true" }), h("span", t("nav.menu")));

  const account = h("button.account-btn", { type: "button", onclick: signOut },
    h("span.account-email", state.user?.email || ""), h("span.account-action", t("nav.signout")));

  return h("header.board-bar",
    h("div.board-bar-inner",
      h("a.brand", { href: "#/overview", "aria-label": t("brand.home") }, flapWord("SPARTON")),
      h("div.nav-drawer#nav-drawer", navEl, h("div.bar-tools", langSwitch(), account)),
      menuBtn));
}

function setMobileNav(app, open) {
  app.dataset.nav = open ? "open" : "closed";
  app.querySelector(".menu-btn")?.setAttribute("aria-expanded", String(open));
  if (open) navEl.querySelector("a")?.focus();
}

addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || document.querySelector("dialog[open]")) return;
  const app = document.querySelector(".app[data-nav='open']");
  if (app) { setMobileNav(app, false); app.querySelector(".menu-btn")?.focus(); }
});

async function signOut() {
  try { await api.logout(); } catch { /* token may already be dead */ }
  token.clear();
  state.user = null;
  toast(t("nav.signedOut"), "info");
  renderAuth();
}

function mountShell() {
  const app = h("div.app", { "data-nav": "closed" });
  contentEl = h("main.content#main", { tabindex: "-1" });
  app.append(buildHeader(app), contentEl);
  fill(root, h("a.skip-link", { href: "#main" }, t("a11y.skip")), app);

  navEl.addEventListener("click", (event) => {
    if (event.target.closest(".nav-item")) setMobileNav(app, false);
  });
  renderRoute();
}

/* ------------------------------------------------------------ route host */
async function renderRoute() {
  const route = routeFromHash();
  for (const link of navEl.querySelectorAll(".nav-item")) {
    if (link.dataset.route === route.id) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = `${t(`nav.${route.id}`)} · Sparton`;

  currentTeardown?.();
  currentTeardown = null;
  for (const el of document.querySelectorAll("dialog[open]")) el.close();

  const view = h("div.view");
  fill(contentEl, view);
  scrollTo(0, 0);

  try {
    const module = await route.load();
    currentTeardown = module.default(view, { navigate, state }) || null;
  } catch (err) {
    const { errorState } = await import("./ui.js");
    fill(view, errorState({ title: t("error.viewLoad"), message: err.message, onRetry: renderRoute }));
  }
}

/* ------------------------------------------------------------- bootstrap */
onUnauthorized.add(() => {
  if (state.user) toast(t("auth.expired"), "danger");
  state.user = null;
  renderAuth();
});

addEventListener("hashchange", () => { if (state.user) renderRoute(); });

/** True when the URL asks for the registration form rather than sign-in. */
const signupRequested = () => /^#\/?(signup|register)(\?|$)/i.test(location.hash);

async function boot() {
  // The landing page sends every prospective customer to `/app/#signup`.
  if (!token.get()) return renderAuth({ mode: signupRequested() ? "register" : "login" });
  try {
    state.user = await api.me();
    mountShell();
  } catch {
    token.clear();
    renderAuth({ mode: signupRequested() ? "register" : "login" });
  }
}

boot();
