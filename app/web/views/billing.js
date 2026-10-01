// Plan, limits and payment. Prices and limits come from the API, never from this file.
// Checkout and the customer portal are Stripe's own pages; we only send people there.

import { api, settleAll, settledValue } from "../api.js";
import { h, fill, button, toast, errorState } from "../ui.js";
import { t, fmtMoney, fmtDate } from "../i18n.js";
import { flapWord } from "../board.js";
import { loadingBoard } from "./overview.js";

export default function billingView(host) {
  const run = async () => {
    fill(host, head(), loadingBoard(2));
    try {
      const [cur, all] = await settleAll([api.plan(), api.plans()]);
      if (cur.status === "rejected") throw cur.reason;
      const { plan, used, subscription, billing_enabled: enabled } = cur.value;
      const plans = settledValue(all, { plans: [] }).plans || [];
      const chosen = sessionStorage.getItem("sparton.plan");

      fill(host, head(),
        h("section.plate.plan-now", { "aria-labelledby": "plan-now-title" },
          h("div.plan-now-head",
            h("div",
              h("p.plan-now-k", t("bl.current")),
              h("h2#plan-now-title", plan.name)),
            flapWord(price(plan), { className: "plan-flaps" })),
          h("div.limits",
            limit(t("bl.shops"), used.shops, plan.shops),
            limit(t("bl.competitors"), used.competitors, plan.competitors)),
          h("p.plan-now-meta", cadence(plan.crawl_frequency_hours),
            subscription?.current_period_end ? ` · ${t(subscription.cancel_at_period_end ? "bl.endsOn" : "bl.renewsOn", { date: fmtDate(new Date(subscription.current_period_end), { day: "numeric", month: "long", year: "numeric" }) })}` : ""),
          subscription && enabled ? portalButton() : null),
        !enabled ? h("p.bl-note", { role: "note" }, t("bl.disabled")) : null,
        h("section.bl-plans", { "aria-labelledby": "bl-plans-title" },
          h("h2#bl-plans-title", t("bl.plans")),
          h("div.plans", plans.map((p) => planCard(p, plan.id, enabled, chosen === p.id)))),
        h("p.bl-fine", t("bl.fine")));
      sessionStorage.removeItem("sparton.plan");
    } catch (err) {
      fill(host, head(), errorState({ title: t("bl.error"), message: err.message, onRetry: run }));
    }
  };
  run();
}

const head = () => h("header.view-head", h("h1.view-title", t("nav.billing")), h("p.view-sub", t("bl.sub")));
const price = (p) => fmtMoney(p.price_cents / 100, "EUR", 0);
function cadence(hours) {
  if (hours <= 12) return t("plan.freq.twiceDaily");
  if (hours <= 24) return t("plan.freq.daily");
  return t("plan.freq.weekly");
}

/** "2 of 3 competitors": a tile per allowance, filled where used. */
function limit(label, used, max) {
  const full = used >= max;
  return h("div.limit", { "data-full": full ? "" : null },
    h("div.limit-tiles", { "aria-hidden": "true" },
      Array.from({ length: Math.min(max, 15) }, (_, i) => h("span.limit-tile", { "data-used": i < used ? "" : null })),
      max > 15 ? h("span.limit-more", `+${max - 15}`) : null),
    h("p", h("strong", t("bl.usedOf", { used, max })), ` ${label}`, full ? h("span.limit-full", ` · ${t("bl.full")}`) : null));
}

function planCard(p, currentId, enabled, highlighted) {
  const current = p.id === currentId;
  const go = current
    ? h("p.plan-current-tag", t("bl.yourPlan"))
    : p.price_cents === 0
      ? null
      : button(t("plan.choose", { name: p.name }), {
          variant: highlighted || p.id === "pro" ? "primary" : undefined,
          disabled: !enabled,
          onClick: async (e) => {
            const b = e.currentTarget;
            b.dataset.loading = "true";
            try {
              const { url } = await api.checkout(p.id);
              location.assign(url);
            } catch (err) { toast(err.message, "danger"); delete b.dataset.loading; }
          },
        });
  return h("article.lp-plan", { "data-featured": highlighted || (!currentId && p.id === "pro") ? "" : null, "data-current": current ? "" : null },
    h("div.plan-head", h("h3.plan-name", p.name)),
    h("p.plan-price", flapWord(price(p), { className: "plan-flaps" }), h("span.plan-per", p.price_cents ? t("plan.perMonth") : t("plan.forever"))),
    h("ul.plan-features",
      h("li", t("plan.shops", { n: p.shops })),
      h("li", t("plan.competitors", { n: p.competitors })),
      h("li", cadence(p.crawl_frequency_hours)),
      h("li", t("plan.history", { n: p.report_history_months })),
      p.features?.api_access ? h("li", t("plan.api")) : null,
      p.features?.priority_support ? h("li", t("plan.support")) : null),
    go);
}

function portalButton() {
  const b = button(t("bl.portal"), { onClick: async () => {
    b.dataset.loading = "true";
    try { location.assign((await api.portal()).url); } catch (err) { toast(err.message, "danger"); delete b.dataset.loading; }
  } });
  return b;
}
