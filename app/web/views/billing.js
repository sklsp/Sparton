// Billing — plan, usage and the upgrade path.
//
// One job: make it obvious what the customer is paying for, what they have
// used, and what happens if they use more. Usage is a meter against the plan's
// hard limit, because a limit you discover by being refused is a limit that
// loses you the customer.
//
// The shapes here mirror the API exactly (`app/billing/plans.py`,
// `app/api/billing.py`):
//   GET /billing/plan  -> { plan, used, remaining, subscription, billing_enabled }
//   GET /billing/plans -> { plans, trial_days, currency }
//   GET /billing/usage -> token spend, not plan quotas
// Note `plan.id`, not `plan.slug`, and `usage` is LLM tokens — the quotas a
// customer actually hits live on `/billing/plan`.

import { api } from "../api.js";
import { h, fill, asyncPanel, toast, badge, button, meter, when, num } from "../ui.js";
import { isPlanLimit, planLimitMessage } from "./shared.js";

const PLAN_ORDER = ["free", "pro", "business"];

export default function billingView(host) {
  const summaryHost = h("div.stack");
  const usageHost = h("div.stack");
  const plansHost = h("div.stack");
  let reload = () => {};

  const manageButton = button("Manage payment method", { onClick: () => portal() });

  host.append(
    h("div.toolbar",
      h("h2.view-title", "Billing"),
      h("div.toolbar-controls",
        manageButton,
        button("Refresh", { onClick: () => reload() }))),
    summaryHost,
    h("section.panel",
      h("h3.panel-title", "Your allowance"),
      h("p.panel-hint",
        "We count what you actually use. When a limit is reached we stop you "
        + "at the door and tell you which plan to move to, rather than "
        + "surprising you with an invoice."),
      usageHost),
    h("section.panel",
      h("h3.panel-title", "Plans"),
      plansHost));

  /* -------------------------------------------------------------- summary */
  function summaryCard(current) {
    const plan = current.plan || {};
    const sub = current.subscription;

    // Free accounts have no subscription row; that is the normal case, not an
    // error, so it gets an upgrade prompt instead of a "manage" link.
    const manage = sub
      ? button("Manage subscription", { onClick: () => portal() })
      : button(
          `Try Pro${current.trial_days ? ` — ${current.trial_days} days free` : ""}`,
          { variant: "primary", onClick: () => checkout("pro") },
        );

    const notes = [];
    if (!current.billing_enabled) {
      notes.push("Payments are not enabled on this deployment, so checkout is "
        + "unavailable. The plan shown is the one applied at signup.");
    }
    if (sub?.cancel_at_period_end) {
      notes.push("This subscription is set to cancel at the end of the period.");
    }
    if (sub?.status && sub.status !== "active") {
      notes.push(`Payment status: ${sub.status.replace("_", " ")}.`);
    }

    return h("div.plan-current", { "data-plan": plan.id || "free" },
      h("div.plan-current-main",
        h("p.plan-current-label", "Your plan"),
        h("p.plan-current-name", plan.name || "Free"),
        h("p.plan-current-price",
          plan.price_cents
            ? h("span", `€${(plan.price_cents / 100).toFixed(0)}`,
                h("span.plan-per", "/ month"))
            : h("span.plan-free", "Free forever")),
        sub
          ? badge(sub.status, sub.status === "active" ? "success" : "warning")
          : badge("Free plan", "neutral")),
      h("div.plan-current-side",
        sub?.current_period_end
          ? h("p.plan-renewal", "Renews ", when(sub.current_period_end))
          : null,
        manage,
        notes.length
          ? h("ul.plan-notes", notes.map((note) => h("li", note)))
          : null));
  }

  /* --------------------------------------------------------------- usage */
  function usageRow(label, used, limit) {
    const fraction = limit > 0 ? Math.min(used / limit, 1) : 0;
    const tone = fraction >= 1 ? "danger" : fraction >= 0.8 ? "warning" : "info";
    return h("div.usage-row",
      h("div.usage-head",
        h("span.usage-label", label),
        h("span.usage-count", `${num(used)} of ${num(limit)}`)),
      meter(fraction, tone));
  }

  function renderUsage(current) {
    const plan = current.plan || {};
    const used = current.used || {};

    const rows = [
      ["Shops", used.shops, plan.shops],
      ["Competitors tracked", used.competitors, plan.competitors],
      ["Daily report tokens", used.daily_tokens, plan.daily_tokens],
    ].filter((row) => row[2] != null);

    const cadence = plan.crawl_frequency_hours
      ? h("p.usage-note", `Scheduled crawls run every ${plan.crawl_frequency_hours} `
          + `hour${plan.crawl_frequency_hours === 1 ? "" : "s"} on this plan.`)
      : null;

    fill(usageHost, rows.length
      ? h("div",
          h("div.usage-list", rows.map(([label, u, limit]) => usageRow(label, u || 0, limit))),
          cadence,
          plan.report_history_months
            ? h("p.usage-note", `Report history: ${plan.report_history_months} month`
                + `${plan.report_history_months === 1 ? "" : "s"}.`)
            : null)
      : h("p.muted", "This plan does not meter usage."));
  }

  /* --------------------------------------------------------------- plans */
  function planCard(plan, currentId, trialDays) {
    const isCurrent = plan.id === currentId;
    const featured = plan.id === "pro";
    const perks = [
      `${plan.shops} shop${plan.shops === 1 ? "" : "s"}`,
      `${plan.competitors} competitor${plan.competitors === 1 ? "" : "s"}`,
      `Crawls every ${plan.crawl_frequency_hours} hour`
        + `${plan.crawl_frequency_hours === 1 ? "" : "s"}`,
      `${num(plan.daily_tokens)} report tokens a day`,
    ];

    return h("article.plan", { "data-featured": featured ? "true" : "false" },
      featured ? h("span.plan-flag", "Most popular") : null,
      h("h4.plan-name", plan.name),
      h("p.plan-price",
        plan.price_cents
          ? h("span", `€${(plan.price_cents / 100).toFixed(0)}`,
              h("span.plan-per", "/ month"))
          : h("span.plan-free", "Free")),
      h("ul.plan-features", perks.map((perk) => h("li", perk))),
      isCurrent
        ? badge("Current plan", "success")
        : button(plan.price_cents ? "Upgrade" : "Switch", {
            variant: featured ? "primary" : "ghost",
            onClick: () => checkout(plan.id, trialDays),
          }));
  }

  function renderPlans(catalogue, currentId, trialDays) {
    if (!catalogue.length) {
      fill(plansHost, h("p.muted", "No plans are available right now."));
      return;
    }
    // Trust the server's order over a hard-coded one, so a new plan shows up
    // without a frontend change.
    const ordered = [...catalogue].sort(
      (a, b) => PLAN_ORDER.indexOf(a.id) - PLAN_ORDER.indexOf(b.id),
    );
    fill(plansHost, h("div.plan-grid",
      ordered.map((plan) => planCard(plan, currentId, trialDays))));
  }

  /* ------------------------------------------------------------- actions */
  // Both Stripe entry points hand off to a Stripe-hosted page. The webhook is
  // what actually changes the plan, so abandoning the tab leaves the account
  // exactly as it was — which is the behaviour we want.
  async function checkout(planId, trialDays) {
    if (!planId) return;
    try {
      const { url } = await api.checkout(planId);
      if (!url) {
        toast("Payments are not enabled on this deployment.", "danger");
        return;
      }
      window.location.href = url;
    } catch (error) {
      toast(isPlanLimit(error) ? planLimitMessage(error) : error.message, "danger");
    }
  }

  async function portal() {
    try {
      const { url } = await api.portal();
      if (!url) {
        toast("Payments are not enabled on this deployment.", "danger");
        return;
      }
      window.location.href = url;
    } catch (error) {
      toast(error.message, "danger");
    }
  }

  /* ----------------------------------------------------------------- load */
  async function render() {
    const [current, catalogue, spend] = await Promise.all([
      api.plan(),
      api.plans().catch(() => ({ plans: [] })),
      api.usage(30).catch(() => null),
    ]);

    fill(summaryHost, summaryCard(current));
    renderUsage(current);
    renderPlans(catalogue.plans || [], current.plan?.id, catalogue.trial_days);

    // Token spend is a separate, advisory number: it is what the LLM cost us,
    // not a quota the customer is up against. Appended, never substituted for
    // the allowance meters.
    if (spend?.total_tokens) {
      usageHost.append(h("p.usage-note",
        "Last 30 days: ", num(spend.requests), " model requests, ",
        num(spend.total_tokens), " tokens"));
    }
  }

  reload = asyncPanel(summaryHost, render, render);
  return reload;
}
