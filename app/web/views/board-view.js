// Board pieces the dashboard views share: a change as a board row, the week label, the counters.

import { h } from "../ui.js";
import { t, fmtMoney, fmtDate, fmtNumber } from "../i18n.js";
import { boardRow, flapWord } from "../board.js";

const pct = (v) => `${v > 0 ? "+" : "−"}${Math.abs(Math.round(v))}%`;

/**
 * "They are 10% cheaper than you", when the product is matched to one of the owner's own.
 * Anything short of a barcode match says how sure it is: a title match is never stated as fact.
 */
export function vsYou(v) {
  if (!v || v.gap_pct == null) return "";
  const gap = Number(v.gap_pct);
  const pct = `${fmtNumber(Math.abs(gap), { maximumFractionDigits: 1 })}%`;
  const line = gap > 0 ? t("gap.cheaper", { pct }) : gap < 0 ? t("gap.dearer", { pct }) : t("gap.same");
  if (v.confidence === "certain") return line;
  return `${line} (${v.confidence === "likely" ? t("gap.likely") : t("gap.possible")})`;
}

/** A change event as board cells + one spoken sentence. `source` is the competitor's data tier. */
export function changeCells(c, source) {
  return { ...cells(c, source), note: vsYou(c.vs_you) };
}

function cells(c, source) {
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
  const cells = changeCells(c, source);
  // Unread is said, not only shown: the lamp is invisible to a screen reader.
  if (!c.acknowledged_at) cells.say = `${t("ch.unread")} ${cells.say}`;
  const li = boardRow(cells);
  li.classList.add("is-link");
  if (!c.acknowledged_at) li.dataset.unread = "";
  const open = h("a.row-open", { href: `#/product?change=${c.id}`, onclick: (e) => { if (onOpen) { e.preventDefault(); onOpen(); } } },
    h("span.visually-hidden", t("ch.open")));
  li.prepend(open);
  if (c.evidence_url) {
    li.append(h("a.row-evidence", { href: c.evidence_url, target: "_blank", rel: "noopener noreferrer", title: t("ch.evidence") },
      h("span.visually-hidden", `${t("ch.evidence")}: ${c.product || ""}`),
      h("span.ico-arrow", { "aria-hidden": "true" })));
  }
  return li;
}

/** What the marks on a board mean; only the marks the board actually shows. */
export function boardLegend(changes, source) {
  const marks = new Set();
  for (const c of changes) {
    marks.add(source.get(c.competitor_id) === "html" ? "extracted" : "exact");
    if (c.kind === "out_of_stock" || c.kind === "removed_product") marks.add("struck");
    if (!c.acknowledged_at) marks.add("unread");
  }
  const item = (cls, key) => h("li.legend-item", h(`span.${cls}`, { "aria-hidden": "true" }), t(key));
  return h("ul.board-legend.app-legend", { "aria-label": t("legend.label") },
    marks.has("exact") ? item("legend-tile.mark-exact", "legend.exact") : null,
    marks.has("extracted") ? item("legend-tile.mark-extracted", "legend.extracted") : null,
    marks.has("struck") ? h("li.legend-item", h("span.legend-struck", { "aria-hidden": "true" }, "ABC"), t("legend.struck")) : null,
    marks.has("unread") ? item("legend-lamp", "legend.unread") : null);
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

/** Counters for a board's title bar: a number on flap tiles, its label in plain text. */
export function boardCounts(items) {
  return h("p.board-counts", items.filter(Boolean).map(([n, key, tone = ""]) =>
    h("span.board-count", { "data-tone": tone },
      flapWord(String(n ?? 0), { label: String(n ?? 0) }),
      h("span", t(Number(n) === 1 ? `${key}.one` : key)))));
}
