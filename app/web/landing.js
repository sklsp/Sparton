// Landing page. The only dynamic part is the pricing table, which is read from
// the API so the page can never advertise a price the server does not enforce.
//
// Everything else is static HTML: the marketing copy does not need JavaScript,
// and a visitor with JS disabled still gets the value proposition.

import { h, fill } from "/app/ui.js";

const plansEl = document.getElementById("plans");

/* ------------------------------------------------------------ theme ----- */
const THEME_KEY = "sparton.theme";

function applyTheme(value) {
  if (value === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", value);
  localStorage.setItem(THEME_KEY, value);
}

applyTheme(localStorage.getItem(THEME_KEY) || "system");

/* ------------------------------------------------------------ pricing --- */
const BLURB = {
  free: "Enough to see whether this works for your shop.",
  pro: "For a shop that takes competition seriously.",
  business: "For multi-store and multi-brand sellers.",
};

function money(cents, planId) {
  if (planId === "free") return "€0";
  return `€${(cents / 100).toFixed(0)}`;
}

function planCard(plan, { featured = false } = {}) {
  const items = [
    `${plan.shops} shop${plan.shops === 1 ? "" : "s"}`,
    `${plan.competitors} competitor${plan.competitors === 1 ? "" : "s"}`,
    plan.crawl_frequency_hours <= 24 ? "Daily crawls" : "Weekly crawls",
    "Weekly AI report",
    "Alerts with evidence links",
    plan.features?.email_digest ? "Email digest" : "In-app alerts",
  ];
  if (plan.features?.priority_support) items.push("Priority support");
  if (plan.features?.api_access) items.push("API access");

  const cta = plan.id === "free"
    ? h("a.btn.btn-primary", { href: "/app/#signup" }, "Start free")
    : h("a.btn.btn-primary", { href: `/app/#signup?plan=${plan.id}` },
        `Choose ${plan.name}`);

  return h("article.lp-plan", featured ? { "data-featured": "true" } : null,
    h("h3.lp-plan-name", plan.name),
    h("p.lp-plan-price",
      money(plan.price_cents, plan.id),
      plan.id === "free" ? null : h("small", " / month")),
    h("p.lp-plan-blurb", BLURB[plan.id] || ""),
    h("ul", items.map((item) => h("li", item))),
    cta);
}

async function loadPlans() {
  try {
    const response = await fetch("/billing/plans", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const { plans } = await response.json();
    if (!Array.isArray(plans) || !plans.length) throw new Error("no plans returned");
    // Pro is the plan we want most people on, so it gets the emphasis.
    fill(plansEl, plans.map((plan) => planCard(plan, { featured: plan.id === "pro" })));
  } catch (error) {
    // Never leave a spinner or a bare error on the pricing section: the copy
    // above it still sells the product, and the CTA above is still reachable.
    fill(plansEl,
      h("p.lp-error", "Pricing is temporarily unavailable. Please try again shortly."),
      h("p", { style: { textAlign: "center" } },
        h("a.btn.btn-primary", { href: "/app/#signup" }, "Start free")));
    console.warn("Could not load plans:", error);
  }
}

loadPlans();

import { langSwitch } from "/app/i18n.js";
document.querySelector(".lp-actions")?.prepend(langSwitch());
