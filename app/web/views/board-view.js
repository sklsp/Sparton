// Board pieces the dashboard views share: a change as a board row, the week label, the counters.

import { h } from "../ui.js";
import { t, lang, fmtMoney, fmtDate } from "../i18n.js";
import { boardRow } from "../board.js";
import NumberFlow, { define } from "../vendor/number-flow.esm.js";

define("number-flow", NumberFlow);

const pct = (v) => `${v > 0 ? "+" : "−"}${Math.abs(Math.round(v))}%`;

/** A change event as board cells + one spoken sentence. `source` is the competitor's data tier. */
export function changeCells(c, source) {
  const who = c.competitor || c.competitor_domain || "—";
  const what = c.product || c.title || "—";
  const was = c.previous_price != null ? fmtMoney(c.previous_price, c.currency) : "";
  const now = c.new_price != null ? fmtMoney(c.new_price, c.currency) : "";
  const base = { a: who, b: what, mark: source === "html" ? "extracted" : "exact", extractedNote: t("src.extracted") };
  switch (c.kind) {
    case "price_decrease":
    case "price_increase": {
      const p = c.delta_pct ?? (c.previous_price ? ((c.new_price - c.previous_price) / c.previous_price) * 100 : 0);
      return { ...base, c: was, d: now, e: pct(p), tone: c.kind === "price_decrease" ? "down" : "up",
        say: t(c.kind === "price_decrease" ? "ch.say.down" : "ch.say.up", { who, what, was, now, pct: pct(p) }) };
    }
    case "out_of_stock":
      return { ...base, c: now, span: t("ch.soldout"), struck: true, tone: "stock", say: t("ch.say.soldout", { who, what }) };
    case "back_in_stock":
      return { ...base, c: now, span: t("ch.back"), tone: "new", say: t("ch.say.back", { who, what }) };
    case "new_product":
      return { ...base, c: now, span: t("ch.new"), tone: "new", say: t("ch.say.new", { who, what, now }) };
    case "removed_product":
      return { ...base, c: was, span: t("ch.removed"), struck: true, say: t("ch.say.removed", { who, what }) };
    default:
      return { ...base, c: now, span: t("ch.other"), say: `${who}: ${what}` };
  }
}

/**
 * A change as a board row that opens its product history. The evidence link sits in its own
 * cell on top of the row link, so both stay reachable by keyboard and pointer.
 */
export function changeRow(c, source, onOpen) {
  const li = boardRow(changeCells(c, source));
  li.classList.add("is-link");
  if (!c.acknowledged_at) li.dataset.unread = "";
  const open = h("a.row-open", { href: `#/product?change=${c.id}`, onclick: (e) => { if (onOpen) { e.preventDefault(); onOpen(); } } },
    h("span.visually-hidden", t("ch.open")));
  li.prepend(open);
  if (c.evidence_url) {
    li.append(h("a.row-evidence", { href: c.evidence_url, target: "_blank", rel: "noopener noreferrer", title: t("ch.evidence") },
      h("span.visually-hidden", `${t("ch.evidence")}: ${c.product || ""}`),
      h("span", { "aria-hidden": "true" }, "↗")));
  }
  return li;
}

/** "Week 40 · 28 Sep – 4 Oct": the one time axis every view lines up on. */
export function weekLabel(date = new Date()) {
  const day = (date.getDay() + 6) % 7; // Monday = 0
  const monday = new Date(date); monday.setDate(date.getDate() - day);
  const sunday = new Date(monday); sunday.setDate(monday.getDate() + 6);
  return `${t("week.label", { n: isoWeek(date) })} · ${fmtDate(monday)} – ${fmtDate(sunday)}`;
}

export function isoWeek(date) {
  const d = new Date(Date.UTC(date.getFullYear(), date.getMonth(), date.getDate()));
  d.setUTCDate(d.getUTCDate() + 4 - (d.getUTCDay() || 7));
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  return Math.ceil(((d - yearStart) / 86400000 + 1) / 7);
}

/** "12 changes" with the number rolling in (NumberFlow), and plural-aware wording. */
export function countFlow(n, key, tone = "") {
  const flow = document.createElement("number-flow");
  flow.locales = lang === "nl" ? "nl-NL" : "en-IE";
  const wrap = h("span.count", { "data-tone": tone },
    flow, " ", h("span.count-label", t(Number(n) === 1 ? `${key}.one` : key)));
  requestAnimationFrame(() => flow.update(Number(n) || 0));
  return wrap;
}
