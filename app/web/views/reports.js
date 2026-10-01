// The weekly reports: a list on the weekly axis, and one report read as the board of its moves
// plus the written summary. Numbers come from the report's facts and its change rows, never
// from the prose.

import { api, settleAll, settledValue } from "../api.js";
import { h, fill, button, toast, errorState } from "../ui.js";
import { t, lang, fmtDate } from "../i18n.js";
import { changeRow, isoWeek, boardCounts } from "./board-view.js";
import { loadingBoard } from "./overview.js";

export default function reportsView(host, { navigate }) {
  const selected = () => new URLSearchParams(location.hash.split("?")[1] || "").get("id");

  const run = async () => {
    fill(host, head(), loadingBoard(4));
    try {
      const [rep, sh] = await settleAll([api.reports(30), api.shops()]);
      if (rep.status === "rejected") throw rep.reason;
      const reports = rep.value.reports || [];
      const shops = settledValue(sh, { shops: [] }).shops || [];
      const id = selected() || reports[0]?.id;
      fill(host, head(writeNow(shops, run)),
        reports.length
          ? h("div.reports-split",
              h("nav.report-list", { "aria-label": t("rp.list") }, h("ol", reports.map((r) => listItem(r, String(r.id) === String(id))))),
              h("div.report-host", await reportBody(id, navigate)))
          : h("section.plate.empty-plate",
              h("h2", t(shops.length ? "rp.empty.title" : "al.empty.noShop")),
              h("p", t(shops.length ? "rp.empty.body" : "al.empty.noShopBody")),
              shops.length ? null : h("a.btn", { href: "#/start", "data-variant": "primary" }, t("ov.first.cta"))));
    } catch (err) {
      fill(host, head(), errorState({ title: t("rp.error"), message: err.message, onRetry: run }));
    }
  };
  run();
}

function head(action = null) {
  return h("header.view-head.view-head-row",
    h("div", h("h1.view-title", t("nav.reports")), h("p.view-sub", t("rp.sub"))), action);
}

function writeNow(shops, reload) {
  if (!shops.length) return null;
  const b = button(t("rp.writeNow"), { onClick: async () => {
    b.dataset.loading = "true";
    try {
      for (const s of shops) await api.generateReport(s.id, 7);
      toast(t("rp.queued"), "success");
      setTimeout(reload, 1500);
    } catch (err) { toast(err.message, "danger"); } finally { delete b.dataset.loading; }
  } });
  return b;
}

const period = (r) => {
  const a = new Date(r.period_start); const b = new Date(r.period_end);
  return `${fmtDate(a)} – ${fmtDate(b, { day: "numeric", month: "short", year: "numeric" })}`;
};

function listItem(r, current) {
  const shop = r.facts?.shop?.name || "";
  return h("li", h("a.report-item", { href: `#/reports?id=${r.id}`, "aria-current": current ? "page" : null },
    h("strong", shop || t("ov.report.untitled")),
    h("span.report-item-period", `${t("week.label", { n: isoWeek(new Date(r.period_end)) })} · ${period(r)}`),
    r.status !== "COMPLETED" ? h("span.report-item-status", t(r.status === "FAILED" ? "rp.failed" : "rp.writing")) : null));
}

async function reportBody(id, navigate) {
  if (!id) return null;
  const [r, ch, comps] = await settleAll([api.report(id), api.changes({ days: 365, limit: 200 }), api.competitors()]);
  if (r.status === "rejected") return errorState({ title: t("rp.error"), message: r.reason.message });
  const report = r.value;
  const facts = report.facts || {};
  const ids = new Set((report.change_ids || []).map(String));
  const changes = (settledValue(ch, { changes: [] }).changes || []).filter((c) => ids.has(String(c.id)));
  const source = new Map((settledValue(comps, { competitors: [] }).competitors || []).map((c) => [c.id, c.data_source]));
  const kinds = facts.by_kind || {};

  return h("article.report", { "aria-labelledby": "report-title" },
    h("header.report-head",
      h("h2#report-title", t("rp.title", { shop: facts.shop?.name || "" })),
      h("p.report-week", `${t("week.label", { n: isoWeek(new Date(report.period_end)) })} · ${period(report)}`)),
    changes.length
      ? h("section.board.week-board", { "aria-label": t("rp.moves") },
          h("div.board-head", h("h3.board-title", t("rp.moves"))),
          boardCounts([
            [facts.total_changes ?? changes.length, "ov.count.changes"],
            kinds.price_decrease ? [kinds.price_decrease, "rp.count.cuts"] : null,
            kinds.price_increase ? [kinds.price_increase, "rp.count.rises"] : null,
            (facts.competitors || []).length ? [facts.competitors.length, "ov.count.watched"] : null,
          ]),
          h("ol.board-rows", changes.map((c) => changeRow(c, source.get(c.competitor_id), () => navigate(`product?change=${c.id}`)))))
      : null,
    report.markdown
      ? h("section.plate.report-prose", { "aria-label": t("rp.summary") },
          h("h3", t("rp.summary")),
          lang !== "en" ? h("p.report-lang", t("rp.englishOnly")) : null,
          markdown(report.markdown))
      : null,
    h("p.report-fine", t("rp.fine")));
}

/* -------------------------------------------------------------- markdown */
// The report prose is our own server's Markdown: headings, bold, lists, tables and links.
// Everything is escaped first; only http(s) links survive.
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const inline = (s) => esc(s)
  .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
  .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

export function markdown(src) {
  const out = [];
  const lines = src.replace(/\r/g, "").split("\n");
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) continue;
    const head = /^(#{1,4})\s+(.*)$/.exec(line);
    if (head) {
      // The report's own H1 repeats the title above it; start the prose at H2.
      if (head[1].length === 1) continue;
      out.push(`<h${head[1].length + 2}>${inline(head[2])}</h${head[1].length + 2}>`);
    } else if (line.startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      i--;
      const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const [hd, , ...body] = rows;
      out.push(`<div class="table-wrap"><table class="readings"><thead><tr>${cells(hd).map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${body.map((r) => `<tr>${cells(r).map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
    } else if (/^[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) items.push(lines[i++].replace(/^[-*]\s+/, ""));
      i--;
      out.push(`<ul>${items.map((x) => `<li>${inline(x)}</li>`).join("")}</ul>`);
    } else {
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  return h("div.prose", { html: out.join("") });
}
