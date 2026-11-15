// billing.js on the DOM fake: the current plan with its usage tiles, the plan cards, Checkout for a free
// account, the portal for a paying one, billing switched off, the return from Stripe Checkout and the
// error state. Fixtures mirror GET /billing/plan (usage_report + subscription + billing_enabled) and
// GET /billing/plans from app/api/billing.py and app/billing/plans.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake.
import billingView from "../../app/web/views/billing.js";
import { t } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom } from "./dom.mjs";

// billing.js also reads location.search/pathname, history.replaceState and sessionStorage, which
// env.mjs does not fake (yet). Give them small local stand-ins.
const replaced = [];
globalThis.history = { replaceState: (_state, _title, url) => replaced.push(url) };
if (!globalThis.sessionStorage) {
  const store = new Map();
  globalThis.sessionStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  };
}

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const tick = () => new Promise((r) => setTimeout(r, 0));

// The plans exactly as Plan.to_dict() returns them (app/billing/plans.py).
const free = { id: "free", name: "Free", price_cents: 0, price_label: "Free", shops: 1, competitors: 3, daily_tokens: 2000, crawl_frequency_hours: 168, report_history_months: 1, features: { weekly_report: true, price_alerts: true, assortment_alerts: true, email_digest: false, api_access: false, priority_support: false } };
const pro = { id: "pro", name: "Pro", price_cents: 2900, price_label: "€29", shops: 3, competitors: 15, daily_tokens: 20000, crawl_frequency_hours: 24, report_history_months: 12, features: { weekly_report: true, price_alerts: true, assortment_alerts: true, email_digest: true, api_access: false, priority_support: false } };
const business = { id: "business", name: "Business", price_cents: 7900, price_label: "€79", shops: 10, competitors: 50, daily_tokens: 100000, crawl_frequency_hours: 12, report_history_months: 36, features: { weekly_report: true, price_alerts: true, assortment_alerts: true, email_digest: true, api_access: true, priority_support: true } };

const current = (plan, used, { subscription = null, enabled = true } = {}) => ({
  plan,
  used: { shops: used[0], competitors: used[1], daily_tokens: 0 },
  remaining: { shops: plan.shops - used[0], competitors: plan.competitors - used[1], daily_tokens: plan.daily_tokens },
  subscription,
  billing_enabled: enabled,
});

function routes(state) {
  return (href, init = {}) => {
    if (href === "/billing/plan") return state.planError ? json({ detail: state.planError }, 500) : json(state.current);
    if (href === "/billing/plans") return json({ plans: [free, pro, business], trial_days: 14, currency: "eur" });
    if (href === "/billing/checkout" && init.method === "POST") {
      state.checkouts.push(JSON.parse(init.body));
      return state.checkoutError ? json({ detail: state.checkoutError }, 409) : json({ url: "https://checkout.stripe.test/s/1" });
    }
    if (href === "/billing/portal" && init.method === "POST") { state.portals += 1; return json({ url: "https://billing.stripe.test/p/1" }); }
    throw new Error(`unexpected fetch ${href}`);
  };
}

async function render(state, search = "") {
  resetEnv(); resetDom(); resetToasts();
  replaced.length = 0;
  location.search = search;
  location.pathname = "/app/";
  setFetch(routes(state));
  const host = document.createElement("main");
  const stop = billingView(host);
  await tick(); await tick();
  return { host, stop };
}

const base = (over = {}) => ({ current: current(free, [1, 2]), checkouts: [], portals: 0, ...over });
const choose = (host, plan) => host.querySelectorAll("button").find((b) => b.textContent.includes(t("plan.choose", { name: plan.name })));

test("shows the current plan, its usage and one card per plan", async () => {
  const { host, stop } = await render(base());
  assert.equal(host.querySelector("#plan-now-title").textContent, t("bl.currentIs", { plan: "Free" }));
  const limits = host.querySelectorAll(".limit");
  assert.equal(limits.length, 2);
  assert.ok(limits[0].textContent.includes(t("bl.usedOf", { used: 1, max: 1 })));
  assert.equal(limits[0].hasAttribute("data-full"), true); // 1 of 1 shops: full
  assert.ok(limits[0].textContent.includes(t("bl.full")));
  assert.ok(limits[1].textContent.includes(t("bl.usedOf", { used: 2, max: 3 })));
  assert.equal(limits[1].hasAttribute("data-full"), false);
  const tiles = limits[1].querySelectorAll(".limit-tile");
  assert.equal(tiles.length, 3);
  assert.equal(tiles.filter((x) => x.hasAttribute("data-used")).length, 2);
  // Free is the current plan: its card says so instead of offering a button.
  assert.ok(host.textContent.includes(t("bl.yourPlan")));
  assert.ok(choose(host, pro));
  assert.ok(choose(host, business));
  assert.equal(host.querySelector(".bl-note"), null); // billing on, no checkout return
  stop();
});

test("choosing a paid plan on a free account starts Stripe Checkout", async () => {
  const state = base();
  const { host, stop } = await render(state);
  choose(host, pro).click();
  await tick(); await tick();
  assert.deepEqual(state.checkouts, [{ plan: "pro" }]);
  assert.equal(location.href, "https://checkout.stripe.test/s/1");
  assert.equal(state.portals, 0);
  stop();
});

test("a paying customer changes plan in the portal, never a second Checkout", async () => {
  const sub = { status: "active", current_period_end: "2026-12-01T00:00:00Z", cancel_at_period_end: false };
  const state = base({ current: current(pro, [2, 9], { subscription: sub }) });
  const { host, stop } = await render(state);
  assert.ok(host.textContent.includes(t("bl.portal")));
  choose(host, business).click();
  await tick(); await tick();
  assert.deepEqual(state.checkouts, []);
  assert.equal(state.portals, 1);
  assert.equal(location.href, "https://billing.stripe.test/p/1");
  stop();
});

test("a failed checkout shows the server's message and re-enables the button", async () => {
  const state = base({ checkoutError: "You already have an active subscription" });
  const { host, stop } = await render(state);
  const b = choose(host, pro);
  b.click();
  await tick(); await tick();
  assert.equal(b.hasAttribute("data-loading"), false);
  assert.ok(document.body.textContent.includes("You already have an active subscription"));
  stop();
});

test("with billing switched off the plan buttons are disabled and a note says why", async () => {
  const state = base({ current: current(free, [0, 0], { enabled: false }) });
  const { host, stop } = await render(state);
  assert.ok(host.textContent.includes(t("bl.disabled")));
  assert.equal(choose(host, pro).hasAttribute("disabled"), true); // h() sets boolean props as attributes
  assert.equal(host.textContent.includes(t("bl.portal")), false);
  stop();
});

test("coming back from Checkout thanks the customer once and drops the query", async () => {
  const { host, stop } = await render(base(), "?checkout=success");
  assert.equal(host.querySelector(".bl-note").textContent, t("bl.paid"));
  assert.deepEqual(replaced, ["/app/" + location.hash]);
  stop(); // stops the follow-up polling while the webhook has not landed yet
});

test("a cancelled Checkout says nothing was charged", async () => {
  const { host, stop } = await render(base(), "?checkout=cancel");
  assert.equal(host.querySelector(".bl-note").textContent, t("bl.notPaid"));
  stop();
});

test("when the plan cannot load the view shows the error state", async () => {
  const { host, stop } = await render(base({ planError: "database down" }));
  assert.ok(host.textContent.includes(t("bl.error")));
  assert.ok(host.textContent.includes("database down"));
  stop();
});
