// Plan, limits and payment. Prices and limits come from the API, never from this file.
// Checkout and the customer portal are Stripe's own pages; we only send people there.

import { api, settleAll, settledValue } from "../api.js";
import { h, fill, button, toast, errorState } from "../ui.js";
import { t, fmtMoney, fmtDate } from "../i18n.js";
import { flapWord } from "../board.js";
import { planBoard, cadence } from "../plans.js";
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
            h("h2#plan-now-title", t("bl.currentIs", { plan: plan.name })),
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
          h("div.plans", plans.map((p) => planCard(p, plan.id, enabled, chosen === p.id, Boolean(subscription) && plan.price_cents > 0)))),
        h("p.bl-fine", t("bl.fine")));
      sessionStorage.removeItem("sparton.plan");
    } catch (err) {
      fill(host, head(), errorState({ title: t("bl.error"), message: err.message, onRetry: run }));
    }
  };
  run();
}

const head = () => h("header.view-head", h("h1.view-title", t("nav.billing")), h("p.view-sub", t("bl.sub")));
const price = (p) => fmtMoney(p.price_cents / 100, "EUR", 0).replace(/\s/g, "");

/** "2 of 3 competitors": a tile per allowance, filled where used. */
function limit(label, used, max) {
  const full = used >= max;
  return h("div.limit", { "data-full": full ? "" : null },
    h("div.limit-tiles", { "aria-hidden": "true" },
      Array.from({ length: Math.min(max, 15) }, (_, i) => h("span.limit-tile", { "data-used": i < used ? "" : null })),
      max > 15 ? h("span.limit-more", `+${max - 15}`) : null),
    h("p", h("strong", t("bl.usedOf", { used, max })), ` ${label}`, full ? h("span.limit-full", ` · ${t("bl.full")}`) : null));
}

// A paying customer changes plan in the portal: Checkout would start a second
// subscription and bill both.
function planCard(p, currentId, enabled, highlighted, subscribed) {
  const current = p.id === currentId;
  const action = current
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
              const { url } = await (subscribed ? api.portal() : api.checkout(p.id));
              location.assign(url);
            } catch (err) { toast(err.message, "danger"); delete b.dataset.loading; }
          },
        });
  return planBoard(p, { featured: highlighted, current, action });
}

function portalButton() {
  const b = button(t("bl.portal"), { onClick: async () => {
    b.dataset.loading = "true";
    try { location.assign((await api.portal()).url); } catch (err) { toast(err.message, "danger"); delete b.dataset.loading; }
  } });
  return b;
}
