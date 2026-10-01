// Signed-out screens: sign in, start free, forgot password, reset password, verify email.
// One layout for all of them: an enamel form plate on the wall, beside a board that shows
// what the visitor is signing up for.

import { api, token } from "./api.js";
import { h, fill, button } from "./ui.js";
import { t, langSwitch } from "./i18n.js";
import { flapWord, boardRow } from "./board.js";
import { fmtMoney } from "./i18n.js";

const root = document.getElementById("root");

/** Which signed-out screen the URL asks for, plus its query (token, plan). */
export function authRoute() {
  const [path, query = ""] = location.hash.replace(/^#\/?/, "").split("?");
  const params = new URLSearchParams(query);
  const view = { signup: "signup", register: "signup", forgot: "forgot", reset: "reset", verify: "verify" }[path] || "login";
  return { view, params };
}

/** Server wording is English-only; map the errors we know onto both languages. */
function errorText(err, view) {
  if (err.status === 401 && view === "login") return t("auth.err.invalid");
  if (err.status === 409) return t("auth.err.taken");
  if (err.status === 429) return t("auth.err.rate");
  if (err.status === 400 && view === "reset") return t("auth.err.resetInvalid");
  if (err.status === 422) return t("auth.err.form");
  if (err.status === 0) return t("auth.err.offline");
  return err.message;
}

let fieldSeq = 0;
function input(label, attrs, hint) {
  const id = `f${++fieldSeq}`;
  const hintId = hint ? `${id}-hint` : null;
  const el = h("input.input", { id, ...attrs, "aria-describedby": hintId });
  const wrap = h("div.field",
    h("label", { for: id }, label),
    el,
    hint ? h("p.field-hint", { id: hintId }, hint) : null);
  return { wrap, el };
}

function passwordInput(label, { autocomplete, minlength, hint }) {
  const f = input(label, { type: "password", name: "password", required: true, minlength, autocomplete }, hint);
  const toggle = h("button.reveal", {
    type: "button", "aria-pressed": "false",
    onclick: () => {
      const show = f.el.type === "password";
      f.el.type = show ? "text" : "password";
      toggle.setAttribute("aria-pressed", String(show));
      toggle.textContent = t(show ? "auth.hide" : "auth.show");
    },
  }, t("auth.show"));
  f.wrap.querySelector("input").after(toggle);
  f.wrap.classList.add("has-reveal");
  return f;
}

function layout(view, ...content) {
  // What the board will look like once it runs: three labelled demo rows.
  const eur = (v) => fmtMoney(v, "EUR");
  const demo = [
    { a: "Kade & Co", b: t("lp.demo.belt"), c: eur(34), d: eur(27.5), e: "−19%", tone: "down" },
    { a: "Noord Supply", b: t("lp.demo.oil"), c: eur(18.95), d: eur(21.5), e: "+13%", tone: "up" },
    { a: "Atelier Vos", b: t("lp.demo.scarf"), c: eur(59), span: t("lp.demo.soldout"), struck: true, tone: "stock" },
  ];
  fill(root, h("div.auth-shell",
    h("a.skip-link", { href: "#main" }, t("a11y.skip")),
    h("header.auth-top",
      h("a.brand", { href: "/" }, flapWord("SPARTON", { label: "Sparton" })),
      langSwitch()),
    h("div.auth-stage",
      h("main.auth-card#main", { tabindex: "-1" }, content),
      h("aside.board.auth-board", { "aria-hidden": "true" },
        h("div.board-head", h("span.board-title", t(view === "signup" ? "auth.board.signup" : "auth.board.login")), h("span.board-demo", t("lp.demo"))),
        h("ol.board-rows", demo.map((r) => boardRow(r)))))));
}

function form(view, fields, submitLabel, onSubmit) {
  const message = h("p.form-error", { role: "alert", hidden: true });
  const submit = button(submitLabel, { variant: "primary", type: "submit", size: "lg" });
  const el = h("form.auth-form", {
    novalidate: false,
    onsubmit: async (event) => {
      event.preventDefault();
      message.hidden = true;
      submit.dataset.loading = "true";
      try {
        await onSubmit();
      } catch (err) {
        fill(message, errorText(err, view));
        message.hidden = false;
        message.focus?.();
      } finally {
        delete submit.dataset.loading;
      }
    },
  }, fields, message, submit);
  return el;
}

const link = (hash, label) => h("a", { href: hash }, label);

/** Render the signed-out screen the URL asks for. `onSignedIn(user)` mounts the dashboard. */
export function renderAuth(onSignedIn) {
  const { view, params } = authRoute();
  ({ login, signup, forgot, reset, verify }[view])(params, onSignedIn);
  document.title = `${t(`auth.title.${view}`)} · Sparton`;
}

function login(_params, onSignedIn) {
  const email = input(t("auth.email"), { type: "email", name: "email", autocomplete: "email", required: true });
  const password = passwordInput(t("auth.password"), { autocomplete: "current-password", minlength: 1 });
  layout("login",
    h("h1", t("auth.loginTitle")),
    h("p.auth-lede", t("auth.loginLede")),
    form("login", [email.wrap, password.wrap, h("p.auth-aside", link("#/forgot", t("auth.forgot")))], t("auth.signin"), async () => {
      const result = await api.login(email.el.value, password.el.value);
      token.set(result.token);
      onSignedIn(result.user);
    }),
    h("p.auth-switch", t("auth.noAccount"), " ", link("#/signup", t("auth.toSignup"))));
  email.el.focus();
}

function signup(params, onSignedIn) {
  const plan = params.get("plan");
  const email = input(t("auth.email"), { type: "email", name: "email", autocomplete: "email", required: true });
  const password = passwordInput(t("auth.password"), { autocomplete: "new-password", minlength: 8, hint: t("auth.passwordHint") });
  const org = input(t("auth.shopName"), { type: "text", name: "organization_name", autocomplete: "organization", required: true });
  layout("signup",
    h("h1", t("auth.signupTitle")),
    h("p.auth-lede", t("auth.signupLede")),
    plan && plan !== "free" ? h("p.auth-note", t("auth.planNote", { plan: plan[0].toUpperCase() + plan.slice(1) })) : null,
    form("signup", [email.wrap, password.wrap, org.wrap], t("auth.create"), async () => {
      const result = await api.register(email.el.value, password.el.value, org.el.value);
      token.set(result.token);
      if (plan && plan !== "free") sessionStorage.setItem("sparton.plan", plan);
      // New accounts go straight into onboarding: the first thing to do is add a shop.
      history.replaceState(null, "", "#/start");
      onSignedIn(result.user);
    }),
    h("p.auth-legal", t("auth.legal.pre"), " ", link("/legal/terms", t("auth.legal.terms")), " ", t("auth.legal.and"), " ", link("/legal/privacy", t("auth.legal.privacy")), "."),
    h("p.auth-switch", t("auth.haveAccount"), " ", link("#/login", t("auth.toLogin"))));
  email.el.focus();
}

function forgot() {
  const email = input(t("auth.email"), { type: "email", name: "email", autocomplete: "email", required: true });
  layout("forgot",
    h("h1", t("auth.forgotTitle")),
    h("p.auth-lede", t("auth.forgotLede")),
    form("forgot", [email.wrap], t("auth.sendLink"), async () => {
      await api.forgotPassword(email.el.value);
      const card = document.querySelector(".auth-card");
      fill(card,
        h("h1", t("auth.checkInbox")),
        h("p.auth-lede", t("auth.sentLede", { email: email.el.value })),
        h("p.auth-switch", link("#/login", t("auth.backToLogin"))));
      card.focus();
    }),
    h("p.auth-switch", link("#/login", t("auth.backToLogin"))));
  email.el.focus();
}

function reset(params) {
  const resetToken = params.get("token") || "";
  if (!resetToken) {
    layout("reset",
      h("h1", t("auth.resetTitle")),
      h("p.form-error", { role: "alert" }, t("auth.err.resetInvalid")),
      h("p.auth-switch", link("#/forgot", t("auth.requestNew"))));
    return;
  }
  const pw = passwordInput(t("auth.newPassword"), { autocomplete: "new-password", minlength: 8, hint: t("auth.passwordHint") });
  const again = passwordInput(t("auth.repeatPassword"), { autocomplete: "new-password", minlength: 8 });
  layout("reset",
    h("h1", t("auth.resetTitle")),
    h("p.auth-lede", t("auth.resetLede")),
    form("reset", [pw.wrap, again.wrap], t("auth.savePassword"), async () => {
      if (pw.el.value !== again.el.value) {
        const err = new Error(t("auth.err.mismatch"));
        err.status = -1;
        again.el.focus();
        throw err;
      }
      await api.resetPassword(resetToken, pw.el.value);
      history.replaceState(null, "", "#/login");
      const card = document.querySelector(".auth-card");
      fill(card,
        h("h1", t("auth.resetDone")),
        h("p.auth-lede", t("auth.resetDoneLede")),
        h("a.btn", { href: "#/login", "data-variant": "primary", "data-size": "lg" }, t("auth.signin")));
      card.focus();
    }),
    h("p.auth-switch", link("#/login", t("auth.backToLogin"))));
  pw.el.focus();
}

async function verify(params, onSignedIn) {
  layout("verify", h("h1", t("auth.verifyTitle")), h("p.auth-lede", { role: "status" }, t("auth.verifying")));
  const card = document.querySelector(".auth-card");
  try {
    await api.verifyEmail(params.get("token") || "");
    fill(card,
      h("h1", t("auth.verified")),
      h("p.auth-lede", t("auth.verifiedLede")),
      token.get()
        ? button(t("auth.toDashboard"), { variant: "primary", size: "lg", onClick: async () => { history.replaceState(null, "", "#/overview"); onSignedIn(await api.me()); } })
        : h("a.btn", { href: "#/login", "data-variant": "primary", "data-size": "lg" }, t("auth.signin")));
  } catch (err) {
    fill(card, h("h1", t("auth.verifyTitle")), h("p.form-error", { role: "alert" }, errorText(err, "verify")));
  }
}
