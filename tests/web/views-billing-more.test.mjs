// Branch coverage for billing.js's untaken sides: the Customer Portal button (success and failure),
// the "ends on" wording when a subscription is cancelled at period end, the "+N" overflow tile for an
// allowance above 15, and the empty plan list fallback. Fixtures mirror GET /billing/plan,
// GET /billing/plans and POST /billing/portal from app/api/billing.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake.
import billingView from "../../app/web/views/billing.js";
import { t, fmtDate } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom } from "./dom.mjs";

// billing.js reads history.replaceState and sessionStorage, which env.mjs does not fake (yet).
globalThis.history = { replaceState: () => {} };
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
    if (href === "/billing/plan") return json(state.current);
    // The handler always sends a plans key; the bare answer exercises the view's `|| []` guard.
    if (href === "/billing/plans") return state.barePlans ? json({ trial_days: 14, currency: "eur" }) : json({ plans: [free, pro, business], trial_days: 14, currency: "eur" });
    if (href === "/billing/portal" && init.method === "POST") {
      state.portals += 1;
      return state.portalError ? json({ detail: state.portalError }, 400) : json({ url: "https://billing.stripe.test/p/1" });
    }
    throw new Error(`unexpected fetch ${href}`);
  };
}

async function render(state) {
  resetEnv(); resetDom(); resetToasts();
  location.search = "";
  location.pathname = "/app/";
  setFetch(routes(state));
  const host = document.createElement("main");
  const stop = billingView(host);
  await tick(); await tick();
  return { host, stop };
}

const base = (over = {}) => ({ current: current(free, [1, 2]), portals: 0, ...over });
const portalButton = (host) => host.querySelectorAll("button").find((b) => b.textContent.includes(t("bl.portal")));


test("the portal button sends a paying customer to the Customer Portal", async () => {
  const sub = { status: "active", current_period_end: "2026-12-01T00:00:00Z", cancel_at_period_end: false };
  const state = base({ current: current(business, [2, 9], { subscription: sub }) });
  const { host, stop } = await render(state);
  const b = portalButton(host);
  assert.ok(b); // paying customer with billing on gets the portal button
  b.click();
  await tick(); await tick();
  assert.equal(state.portals, 1);
  assert.equal(location.href, "https://billing.stripe.test/p/1");
  assert.equal(b.hasAttribute("data-loading"), true); // success keeps it: the page navigates away
  stop();
});

test("a failed portal call shows the server message and re-enables the button", async () => {
  const sub = { status: "active", current_period_end: "2026-12-01T00:00:00Z", cancel_at_period_end: false };
  const state = base({ current: current(business, [2, 9], { subscription: sub }), portalError: "This account has no subscription to manage" });
  const { host, stop } = await render(state);
  const b = portalButton(host);
  assert.ok(b);
  b.click();
  await tick(); await tick();
  assert.equal(b.hasAttribute("data-loading"), false);
  assert.ok(document.body.textContent.includes("This account has no subscription to manage"));
  stop();
});

test("a subscription cancelled at period end says when it ends, not when it renews", async () => {
  const sub = { status: "active", current_period_end: "2026-12-01T00:00:00Z", cancel_at_period_end: true };
  const state = base({ current: current(pro, [2, 9], { subscription: sub }) });
  const { host, stop } = await render(state);
  const meta = host.querySelector(".plan-now-meta");
  const date = fmtDate(new Date(sub.current_period_end), { day: "numeric", month: "long", year: "numeric" });
  assert.ok(meta.textContent.includes(t("bl.endsOn", { date })));
  assert.ok(!meta.textContent.includes(t("bl.renewsOn", { date })));
  stop();
});

test("an allowance above 15 shows 15 tiles plus the overflow count", async () => {
  const state = base({ current: current(business, [2, 9]) });
  const { host, stop } = await render(state);
  const limits = host.querySelectorAll(".limit");
  assert.equal(limits.length, 2);
  assert.equal(limits[0].querySelector(".limit-more"), null); // shops: max 10 fits in tiles
  assert.equal(limits[1].querySelectorAll(".limit-tile").length, 15); // competitors: capped at 15 tiles
  assert.equal(limits[1].querySelector(".limit-more").textContent, "+35"); // 50 - 15
  stop();
});

test("when the plan list answer has no plans key the view renders without cards", async () => {
  const state = base({ barePlans: true });
  const { host, stop } = await render(state);
  assert.equal(host.querySelectorAll(".plan-board").length, 0);
  assert.ok(host.querySelector(".bl-fine")); // the rest of the view still renders
  stop();
});

