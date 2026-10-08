// Sparton dashboard shell: session, the board header, navigation, routing.

import { api, token, onUnauthorized } from "./api.js";
import { h, fill, button, toast } from "./ui.js";
import { renderAuth, authRoute } from "./auth.js";
import { ROUTES } from "./routes.js";
import { t, lang, langSwitch } from "./i18n.js";
import { flapWord } from "./board.js";

const root = document.getElementById("root");

export const state = { user: null };

/* ------------------------------------------------------------- routing */
const routeFromHash = () => {
  const id = location.hash.replace(/^#\/?/, "").split("?")[0] || "overview";
  return ROUTES.find((r) => r.id === id) || ROUTES[0];
};

export const navigate = (id) => { location.hash = `#/${id}`; };

/* --------------------------------------------------------------- signed out */
function signedOut() {
  state.user = null;
  renderAuth((user) => { state.user = user; mountShell(); });
}

/* ---------------------------------------------------------------- shell */
let contentEl;
let navEl;
let currentTeardown = null;

function buildHeader(app) {
  navEl = h("nav.nav", { "aria-label": t("nav.label") },
    ROUTES.filter((r) => r.nav !== false).map((r) => h("a.nav-item", { href: `#/${r.id}`, "data-route": r.id }, t(`nav.${r.id}`))));

  const menuBtn = h("button.menu-btn", {
    type: "button", "aria-controls": "nav-drawer", "aria-expanded": "false",
    onclick: () => setMobileNav(app, app.dataset.nav !== "open"),
  }, h("span.menu-bars", { "aria-hidden": "true" }), h("span", t("nav.menu")));

  const account = h("button.account-btn", { type: "button", onclick: signOut },
    h("span.account-email", state.user?.email || ""), h("span.account-action", t("nav.signout")));

  return h("header.board-bar",
    h("div.board-bar-inner",
      h("a.brand", { href: "#/overview" }, flapWord("SPARTON", { label: "Sparton" })),
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
  history.replaceState(null, "", "#/login");
  signedOut();
}

function mountShell() {
  const app = h("div.app", { "data-nav": "closed" });
  contentEl = h("main.content#main", { tabindex: "-1" });
  app.append(buildHeader(app));
  if (state.user && !state.user.email_verified && !sessionStorage.getItem("sparton.verifyHidden")) app.append(verifyBanner());
  app.append(contentEl);
  fill(root, h("a.skip-link", { href: "#main" }, t("a11y.skip")), app);

  navEl.addEventListener("click", (event) => {
    if (event.target.closest(".nav-item")) setMobileNav(app, false);
  });
  renderRoute();
}

/** Unverified accounts can sign in but not use the product in production: say so, with the fix. */
function verifyBanner() {
  const resend = button(t("verify.resend"), { size: "sm", onClick: async () => {
    resend.dataset.loading = "true";
    try {
      await api.resendVerification();
      fill(note, t("verify.sent"));
      resend.remove();
    } catch (err) {
      fill(note, err.message);
    } finally { delete resend.dataset.loading; }
  } });
  const note = h("span", t("verify.banner", { email: state.user.email }));
  const hide = h("button.verify-hide", { type: "button", onclick: (e) => {
    sessionStorage.setItem("sparton.verifyHidden", "1");
    e.currentTarget.closest(".verify-banner").remove();
  } }, t("verify.hide"));
  return h("div.verify-banner", { role: "status" }, h("div.verify-inner", note, h("span.verify-actions", resend, hide)));
}

/* ------------------------------------------------------------ route host */
async function renderRoute() {
  const route = routeFromHash();
  for (const link of navEl.querySelectorAll(".nav-item")) {
    if (link.dataset.route === (route.parent || route.id)) link.setAttribute("aria-current", "page");
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
  signedOut();
});

const AUTH_ONLY = new Set(["login", "signup", "forgot", "reset"]);

const isVerifyLink = () => /^#\/?verify/.test(location.hash);

addEventListener("hashchange", () => {
  if (state.user && !isVerifyLink()) renderRoute();
  else signedOut();
});

async function boot() {
  // The landing page sends every prospective customer to `/app/#signup`; email links land on
  // `#/verify` and `#/reset`. Signed-out screens own those hashes.
  if (!token.get()) return signedOut();
  if (isVerifyLink()) return signedOut();
  try {
    state.user = await api.me();
    // Stripe sends customers back to /app/?checkout=... (Checkout) or /app/?tab=billing (portal).
    const fromStripe = new URLSearchParams(location.search);
    if (!location.hash && (fromStripe.has("checkout") || fromStripe.get("tab") === "billing")) {
      history.replaceState(null, "", `${location.pathname}${location.search}#/billing`);
    }
    if (AUTH_ONLY.has(authRoute().view) && /^#\/?(login|signup|register|forgot|reset)/.test(location.hash)) {
      history.replaceState(null, "", "#/overview");
    }
    mountShell();
  } catch {
    token.clear();
    signedOut();
  }
}

boot();
