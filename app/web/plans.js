// A plan as a small board: name and price on tiles, its limits as flap rows, the extras in chalk.
// Shared by the landing page and Billing. Every number comes from the plan object (the API).

import { t, fmtMoney } from "./i18n.js";
import { flapWord } from "./board.js";

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

export function cadence(hours) {
  if (hours <= 12) return t("plan.freq.twiceDaily");
  if (hours <= 24) return t("plan.freq.daily");
  return t("plan.freq.weekly");
}

const checks = (hours) => (hours <= 12 ? "2×/D" : hours <= 24 ? "1×/D" : "1×/W");

/** `action` is the element under the board: a link, a button or a "current plan" note. */
export function planBoard(plan, { featured = false, current = false, action = null } = {}) {
  const card = el("article", "lp-plan plan-board");
  if (featured) card.dataset.featured = "";
  if (current) card.dataset.current = "";

  const head = el("div", "plan-head");
  head.append(el("h3", "plan-name", plan.name));
  if (featured) head.append(el("span", "plan-flag", t("plan.mostShops")));

  const price = el("p", "plan-price");
  // No spacer tile between "€" and the figure: a blank flap reads as one that failed to turn.
  const amount = fmtMoney(plan.price_cents / 100, "EUR", 0).replace(/\s/g, "");
  price.append(flapWord(amount, { className: "plan-flaps", label: amount }),
    el("span", "plan-per", plan.price_cents ? t("plan.perMonth") : t("plan.forever")));

  const rows = el("dl", "plan-rows");
  for (const [label, value, spoken] of [
    [t("plan.row.shops"), String(plan.shops), t("plan.shops", { n: plan.shops })],
    [t("plan.row.rivals"), String(plan.competitors), t("plan.competitors", { n: plan.competitors })],
    [t("plan.row.checks"), checks(plan.crawl_frequency_hours), cadence(plan.crawl_frequency_hours)],
  ]) {
    const row = el("div", "plan-row");
    const dd = el("dd");
    dd.append(flapWord(value, { label: spoken }));
    row.append(el("dt", null, label), dd);
    rows.append(row);
  }

  const extras = el("ul", "plan-features");
  for (const item of [
    t("plan.history", { n: plan.report_history_months }),
    t("plan.weeklyReport"),
    plan.features?.api_access ? t("plan.api") : null,
    plan.features?.priority_support ? t("plan.support") : null,
  ].filter(Boolean)) extras.append(el("li", null, item));

  card.append(head, price, rows, extras);
  if (action) card.append(action);
  return card;
}
