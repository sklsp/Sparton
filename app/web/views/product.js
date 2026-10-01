// One product at one competitor: its price over time, every reading, and the evidence.
// Grammar shared by every price history: your price a solid line, competitors dashed;
// a filled point is an exact reading, a hollow one was extracted from the page.

import { api } from "../api.js";
import { h, fill, errorState } from "../ui.js";
import { t, fmtMoney, fmtDate } from "../i18n.js";
import { boardRow } from "../board.js";
import { changeCells, isoWeek } from "./board-view.js";
import { loadingBoard } from "./overview.js";

const SVG = "http://www.w3.org/2000/svg";
const s = (tag, attrs = {}, ...kids) => {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  el.append(...kids);
  return el;
};

export default function productView(host) {
  const id = new URLSearchParams(location.hash.split("?")[1] || "").get("change");
  let onResize = null;

  const run = async () => {
    fill(host, h("p.back", h("a", { href: "#/overview" }, h("span.ico-arrow.ico-back", { "aria-hidden": "true" }), " ", t("pd.back"))), loadingBoard(2));
    try {
      const data = await api.changeHistory(id);
      // Opening a change is reading it.
      if (!data.change.acknowledged_at) api.acknowledgeChange(id).catch(() => {});
      const competitors = await api.competitors().catch(() => ({ competitors: [] }));
      const comp = (competitors.competitors || []).find((c) => c.id === data.change.competitor_id);
      const chartHost = h("div.chart-host");
      fill(host,
        h("p.back", h("a", { href: "#/overview" }, h("span.ico-arrow.ico-back", { "aria-hidden": "true" }), " ", t("pd.back"))),
        h("header.view-head",
          h("h1.view-title", data.product || t("pd.untitled")),
          h("p.view-sub", data.competitor, comp ? ` · ${t(comp.data_source === "html" ? "src.extracted" : "src.exact")}` : "")),
        h("section.board.pd-change", { "aria-label": t("pd.change") },
          h("ol.board-rows", boardRow(changeCells(data.change, comp?.data_source)))),
        h("section.plate.pd-chart", { "aria-labelledby": "pd-chart-title" },
          h("h2#pd-chart-title", t("pd.chart.title")),
          legend(),
          chartHost),
        readings(data));
      const draw = () => fill(chartHost, chart(data.points, data.currency, chartHost.clientWidth || 640));
      draw();
      onResize = () => draw();
      addEventListener("resize", onResize);
    } catch (err) {
      fill(host, h("p.back", h("a", { href: "#/overview" }, h("span.ico-arrow.ico-back", { "aria-hidden": "true" }), " ", t("pd.back"))),
        errorState({ title: t("pd.error"), message: err.status === 404 ? t("pd.notFound") : err.message, onRetry: run }));
    }
  };
  run();
  return () => onResize && removeEventListener("resize", onResize);
}

function legend() {
  return h("ul.chart-legend",
    h("li", h("span.swatch.swatch-solid", { "aria-hidden": "true" }), t("pd.legend.yours"), h("span.legend-note", t("pd.legend.yoursNote"))),
    h("li", h("span.swatch.swatch-dashed", { "aria-hidden": "true" }), t("pd.legend.theirs")),
    h("li", h("span.pt-dot.dot-exact", { "aria-hidden": "true" }), t("pd.legend.exact")),
    h("li", h("span.pt-dot.dot-extracted", { "aria-hidden": "true" }), t("pd.legend.extracted")));
}

function chart(points, currency, width) {
  const priced = points.filter((p) => p.price != null);
  if (!priced.length) return h("p.chart-empty", t("pd.chart.none"));
  const W = Math.max(300, Math.round(width));
  const H = W < 520 ? 220 : 280;
  const pad = { l: W < 520 ? 52 : 64, r: 16, t: 16, b: 34 };
  const times = priced.map((p) => new Date(p.captured_at).getTime());
  const prices = priced.map((p) => Number(p.price));
  let t0 = Math.min(...times); let t1 = Math.max(...times);
  if (t1 - t0 < 7 * 864e5) { t0 -= 3.5 * 864e5; t1 += 3.5 * 864e5; }
  // Round axis steps (1, 2, 2.5 or 5 × 10^k), like the figures on a printed timetable.
  const raw = Math.max(...prices) - Math.min(...prices) || Math.max(...prices) * 0.2 || 1;
  const mag = 10 ** Math.floor(Math.log10(raw / 3));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => raw / s <= 3);
  const lo = Math.max(0, Math.floor(Math.min(...prices) / step) * step - (Math.min(...prices) % step === 0 ? step : 0));
  const hi = Math.ceil(Math.max(...prices) / step) * step + (Math.max(...prices) % step === 0 ? step : 0);
  const x = (v) => pad.l + ((v - t0) / (t1 - t0)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);

  const grid = s("g", { class: "chart-grid" });
  // The weekly ruling: one vertical line per Monday, the same axis as the rest of Sparton.
  const monday = new Date(t0); monday.setHours(0, 0, 0, 0);
  monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7) + 7);
  const weeks = [];
  for (let d = new Date(monday); d.getTime() <= t1; d.setDate(d.getDate() + 7)) weeks.push(new Date(d));
  const every = Math.ceil(weeks.length / (W < 520 ? 4 : 8)) || 1;
  weeks.forEach((d, i) => {
    const gx = x(d.getTime());
    grid.append(s("line", { x1: gx, x2: gx, y1: pad.t, y2: H - pad.b }));
    if (i % every === 0) grid.append(s("text", { x: gx + 4, y: H - pad.b + 18, class: "chart-tick" }, t("week.short", { n: isoWeek(d) })));
  });
  for (let v = lo; v <= hi + step / 2; v += step) {
    const gy = y(v);
    grid.append(s("line", { x1: pad.l, x2: W - pad.r, y1: gy, y2: gy, class: "chart-hline" }),
      s("text", { x: pad.l - 8, y: gy + 4, "text-anchor": "end", class: "chart-tick" }, fmtMoney(v, currency, Number.isInteger(v) ? 0 : 2)));
  }

  // Prices hold until the next reading changes them: a step line, not a slope that never happened.
  let d = `M${x(times[0])},${y(prices[0])}`;
  for (let i = 1; i < priced.length; i++) d += ` H${x(times[i])} V${y(prices[i])}`;
  d += ` H${x(Math.max(t1, times.at(-1)))}`;
  const line = s("path", { d, class: "line-theirs" });

  const dots = s("g");
  priced.forEach((p, i) => {
    const exact = p.data_source === "feed" || p.data_source === "jsonld";
    const cx = x(times[i]); const cy = y(prices[i]);
    dots.append(s("circle", { cx, cy, r: 5, class: exact ? "pt-exact" : "pt-extracted" },
      s("title", {}, `${fmtDate(new Date(p.captured_at), { day: "numeric", month: "short", year: "numeric" })}: ${fmtMoney(p.price, currency)}`)));
    if (p.in_stock === false) dots.append(s("path", { d: `M${cx - 6},${cy - 10} l12,0`, class: "pt-struck" }));
  });

  const summary = t("pd.chart.summary", {
    n: priced.length,
    first: fmtMoney(prices[0], currency),
    last: fmtMoney(prices.at(-1), currency),
  });
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": summary }, grid, line, dots);
  return [svg, priced.length < 2 ? h("p.chart-note", t("pd.chart.one")) : null];
}

function readings(data) {
  const rows = [...data.points].reverse();
  return h("section.plate.pd-readings", { "aria-labelledby": "pd-readings-title" },
    h("h2#pd-readings-title", t("pd.readings.title")),
    rows.length
      ? h("div.table-wrap", h("table.readings",
          h("thead", h("tr",
            h("th", { scope: "col" }, t("pd.col.date")),
            h("th.num", { scope: "col" }, t("pd.col.price")),
            h("th.num", { scope: "col" }, t("pd.col.was")),
            h("th", { scope: "col" }, t("pd.col.stock")),
            h("th", { scope: "col" }, t("pd.col.source")),
            h("th", { scope: "col" }, h("span.visually-hidden", t("ch.evidence"))))),
          h("tbody", rows.map((p) => h("tr",
            h("td", fmtDate(new Date(p.captured_at), { day: "numeric", month: "short", year: "numeric" })),
            h("td.num.tnum", fmtMoney(p.price, data.currency)),
            h("td.num.tnum", p.compare_at_price ? fmtMoney(p.compare_at_price, data.currency) : "—"),
            h("td", t(p.in_stock === false ? "pd.stock.out" : "pd.stock.in")),
            h("td", h("span.src-tag", { "data-src": p.data_source === "html" ? "extracted" : "exact" }, t(p.data_source === "html" ? "src.extracted" : "src.exact"))),
            h("td", p.source_url ? h("a", { href: p.source_url, target: "_blank", rel: "noopener noreferrer" }, t("ch.evidence")) : "—"))))))
      : h("p", t("pd.chart.none")));
}
