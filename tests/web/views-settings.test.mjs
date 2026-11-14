// settings.js on the DOM fake: the account block (email, verified state), changing the password
// (wrong current password refused inline, success clears the form), switching language, signing
// out, and the weekly digest toggle — gated by plan features for free accounts. Fixtures mirror
// the API shapes built in app/api/auth.py (_user_dict) and app/billing/plans.py (usage_report).
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import settingsView from "../../app/web/views/settings.js";
import { t } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch, reloadCalls } from "./env.mjs";
import { resetDom, Event } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// The account as /auth/me returns it (app/api/auth.py _user_dict); the view keeps a copy in state.
const who = { id: 1, email: "bakker@devries.nl", role: "owner", organization_id: 1, email_verified: true, language: "en", weekly_digest_enabled: false };

// The plan as /billing/plan returns it (app/billing/plans.py usage_report + app/api/billing.py
// current_plan). Free has no email digest, so its settings page swaps the toggle for an upgrade hint.
const freePlan = {
  plan: { id: "free", name: "Free", price_cents: 0, price_label: "Free", shops: 1, competitors: 3, daily_tokens: 2000, crawl_frequency_hours: 168, report_history_months: 1, features: { weekly_report: true, price_alerts: true, assortment_alerts: true, email_digest: false, api_access: false, priority_support: false } },
  used: { shops: 1, competitors: 3, daily_tokens: 0 },
  remaining: { shops: 0, competitors: 0, daily_tokens: 2000 },
  subscription: null,
  billing_enabled: false,
};
const proPlan = {
  plan: { id: "pro", name: "Pro", price_cents: 2900, price_label: "€29", shops: 3, competitors: 15, daily_tokens: 20000, crawl_frequency_hours: 24, report_history_months: 12, features: { weekly_report: true, price_alerts: true, assortment_alerts: true, email_digest: true, api_access: false, priority_support: false } },
  used: { shops: 2, competitors: 7, daily_tokens: 0 },
  remaining: { shops: 1, competitors: 8, daily_tokens: 20000 },
  subscription: null,
  billing_enabled: false,
};

// Render the settings view against scripted routes; record every call and posted body.
function renderSettings({ plan = freePlan, changePassword = null, updateSettings = null } = {}) {
  resetEnv();
  resetDom();
  resetToasts(); // ui.js caches its toasts host; a fresh document needs a fresh one.
  const state = { user: { ...who }, lang: "en" };
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  const pwBodies = [];
  const settingsBodies = [];
  const langBodies = [];
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (method === "GET" && href === "/billing/plan") return json(plan);
    if (method === "POST" && href === "/auth/change-password") {
      pwBodies.push(JSON.parse(init.body));
      return changePassword ? Promise.resolve(changePassword()) : json({ changed: true });
    }
    if (method === "PATCH" && href === "/auth/settings") {
      settingsBodies.push(JSON.parse(init.body));
      const body = JSON.parse(init.body);
      return updateSettings ? Promise.resolve(updateSettings()) : json({ ...who, weekly_digest_enabled: body.weekly_digest_enabled });
    }
    if (method === "PUT" && href === "/auth/language") {
      langBodies.push(JSON.parse(init.body));
      return json({ language: JSON.parse(init.body).language });
    }
    if (method === "POST" && href === "/auth/logout") return json({ revoked: true });
    throw new Error(`unexpected fetch ${method} ${href}`);
  });
  settingsView(host, { state });
  return { host, state, calls, pwBodies, settingsBodies, langBodies };
}

// Poll one macrotask at a time until the view has settled; never a fixed sleep.
async function waitFor(fn, what) {
  for (let i = 0; i < 100; i++) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error(`timed out waiting for ${what}`);
}

const digestSection = (host) => [...host.querySelectorAll(".settings-block")].find((b) => b.querySelector("#st-digest"));

test("the account block shows the email and verification state from /auth/me", () => {
  const { host } = renderSettings();
  assert.equal(host.querySelector("#st-account").textContent, t("st.account"));
  const [emailRow, verifiedRow] = [...host.querySelector(".kv-list").children].map((row) => [row.querySelector("dt"), row.querySelector("dd")]);
  assert.equal(emailRow[0].textContent, t("auth.email"));
  assert.equal(emailRow[1].textContent, who.email);
  assert.equal(verifiedRow[0].textContent, t("st.verified"));
  assert.equal(verifiedRow[1].textContent, t("st.yes")); // the fixture is verified
});

test("saving a new password posts both values and clears the form", async () => {
  const { host, pwBodies } = renderSettings();
  const form = host.querySelector("form.st-form");
  form.querySelector("#st-cur").value = "old-password";
  form.querySelector("#st-new").value = "new-password-123";
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => document.querySelector(".toast"), "the saved toast");
  assert.deepEqual(pwBodies, [{ current_password: "old-password", new_password: "new-password-123" }]);
  assert.equal(form.querySelector("#st-cur").value, ""); // e.target.reset() cleared the form
  assert.equal(form.querySelector("#st-new").value, "");
  const toast = document.querySelector(".toast");
  assert.equal(toast.dataset.tone, "success");
  assert.equal(toast.querySelector("div").textContent, t("st.passwordSaved"));
});

test("a wrong current password is refused inline without a toast", async () => {
  const { host } = renderSettings({ changePassword: () => json({ detail: "Current password is incorrect." }, 400) });
  const form = host.querySelector("form.st-form");
  form.querySelector("#st-cur").value = "wrong";
  form.querySelector("#st-new").value = "new-password-123";
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => form.querySelector(".form-error").textContent !== "", "the inline refusal");
  assert.equal(form.querySelector(".form-error").textContent, t("st.wrongPassword")); // a 400 maps to the friendly message
  assert.equal(form.querySelector("#st-cur").value, "wrong"); // not cleared on failure
  assert.equal(document.querySelector(".toast"), null);
});

test("switching language stores it, updates the account and reloads", async () => {
  const { host, calls, langBodies } = renderSettings();
  localStorage.setItem("sparton.token", "test-token"); // signed in: the account follows
  [...host.querySelector(".lang-switch").children].find((b) => b.textContent === "NL").click();
  await waitFor(() => calls.some(([m, h]) => m === "PUT" && h === "/auth/language"), "the language update");
  assert.equal(localStorage.getItem("sparton.lang"), "nl");
  assert.deepEqual(langBodies, [{ language: "nl" }]);
  assert.equal(reloadCalls(), 1); // the page reloads to re-render in Dutch
});

test("signing out logs out, drops the token and lands on the login page", async () => {
  const { host, calls } = renderSettings();
  localStorage.setItem("sparton.token", "test-token");
  host.querySelector('section[aria-labelledby="st-out"]').querySelector('button[data-variant="danger"]').click();
  await waitFor(() => localStorage.getItem("sparton.token") === null && location.hash === "#/login", "sign-out to finish");
  assert.deepEqual(calls.filter(([m]) => m === "POST"), [["POST", "/auth/logout"]]);
  assert.equal(reloadCalls(), 1);
});

test("the digest toggle saves through PATCH /auth/settings and updates the state", async () => {
  const { host, state, settingsBodies } = renderSettings({ plan: proPlan });
  const box = host.querySelector("#st-digest-toggle"); // present synchronously; Pro keeps it after the gate settles
  assert.ok(!box.checked); // the fixture has it off
  box.checked = true;
  box.dispatchEvent(new Event("change"));
  await waitFor(() => document.querySelector(".toast"), "the saved toast");
  assert.deepEqual(settingsBodies, [{ weekly_digest_enabled: true }]);
  assert.equal(state.user.weekly_digest_enabled, true); // the view patches its copy of the account
  const toast = document.querySelector(".toast");
  assert.equal(toast.dataset.tone, "success");
  assert.equal(toast.querySelector("div").textContent, t("st.digestSaved"));
});

test("free accounts see an upgrade hint instead of the digest toggle", async () => {
  const { host } = renderSettings(); // freePlan is the default: no email_digest feature
  await waitFor(() => digestSection(host).querySelector("#st-digest-toggle") === null, "the plan gate to swap in the hint");
  const section = digestSection(host);
  assert.equal(section.querySelector("input"), null);
  assert.ok(section.textContent.includes(t("st.digestPlan")));
});

test("a failed digest save reverts the switch and toasts the error", async () => {
  const { host, state } = renderSettings({ plan: proPlan, updateSettings: () => json({ detail: "The server hiccuped." }, 500) });
  const box = host.querySelector("#st-digest-toggle");
  box.checked = true;
  box.dispatchEvent(new Event("change"));
  await waitFor(() => document.querySelector(".toast"), "the error toast");
  assert.equal(box.checked, false); // reverted
  assert.equal(state.user.weekly_digest_enabled, false); // state untouched on failure
  const toast = document.querySelector(".toast");
  assert.equal(toast.dataset.tone, "danger");
  assert.equal(toast.querySelector("div").textContent, "The server hiccuped.");
});
