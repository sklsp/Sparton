// Reports — the "what changed and what should I do about it" screen.
//
// A report is a generated artefact, so the list is a filing cabinet and the
// detail is the document. Reports are immutable once written: we keep the
// numbers that were true at generation time, even if a later crawl contradicts
// them, because a report you cannot trust retrospectively is worthless.

import { api } from "../api.js";
import { h, fill, asyncPanel, empty, toast, ago, when, badge, button } from "../ui.js";
import { money, kindLabel, kindTone, isPlanLimit, planLimitMessage } from "./shared.js";

const DAYS = [7, 14, 30];

export default function reportsView(host) {
  const listHost = h("div.stack");
  const detailHost = h("div");
  let selectedId = null;
  let reload = () => {};

  // Each picker lives in exactly one place in the tree; a DOM node cannot be
  // in two parents, so there is one shop picker and one period picker.
  const periodSelect = h("select.input", {
    "aria-label": "Reporting period",
  }, DAYS.map((d) => h("option", { value: String(d), selected: d === 30 }, `Last ${d} days`)));

  const shopSelect = h("select.input", {
    "aria-label": "Shop",
  }, h("option", { value: "" }, "Loading shops…"));

  const generateButton = button("Write report", {
    variant: "primary",
    onClick: () => generate(),
  });

  const page = h("div",
    h("div.toolbar",
      h("h2.view-title", "Reports"),
      h("div.toolbar-controls",
        h("label.inline-field", "Period", periodSelect),
        button("Refresh", { onClick: () => reload() }))),
    h("section.panel",
      h("h3.panel-title", "Generate a report"),
      h("p.panel-hint",
        "A report summarises every change we detected in the period: price "
        + "moves, new and delisted products, and stock-outs. Every figure comes "
        + "from our own capture history, so a report is always reproducible."),
      h("div.form-grid",
        h("label.field", h("span", "Shop"), shopSelect),
        h("div.form-actions", generateButton))),
    h("div.reports-split",
      h("section.reports-list-wrap", listHost),
      detailHost));

  host.append(page);

  /* ------------------------------------------------------------- generate */
  async function generate() {
    const shopId = Number(shopSelect.value);
    if (!shopId) {
      toast("Add a shop before generating a report.", "info");
      return;
    }
    const days = Number(periodSelect.value);
    generateButton.disabled = true;
    generateButton.textContent = "Writing…";
    try {
      const result = await api.generateReport(shopId, days);
      selectedId = result?.report?.id ?? result?.id ?? null;
      toast("Report ready", "success");
      reload();
    } catch (error) {
      toast(isPlanLimit(error) ? planLimitMessage(error) : error.message, "danger");
    } finally {
      generateButton.disabled = false;
      generateButton.textContent = "Write report";
    }
  }

  /* ----------------------------------------------------------------- list */
  function reportRow(report) {
    const selected = report.id === selectedId;
    return h("li.report-row", { "data-selected": selected ? "true" : "false" },
      h("button.report-row-btn", {
        onclick: () => { selectedId = report.id; render(); },
        "aria-current": selected ? "true" : "false",
      },
        h("span.report-row-title", report.title || "Untitled report"),
        h("span.report-row-meta",
          h("span", when(report.period_end || report.generated_at)),
          h("span.report-row-stats",
            `${report.change_count} changes`,
            report.competitors_covered != null
              ? ` · ${report.competitors_covered} competitors`
              : ""),
          badge(report.status === "FAILED" ? "failed" : "ready",
            report.status === "FAILED" ? "danger" : "neutral"))));
  }

  function section(heading, ...body) {
    return h("section.report-section", h("h4", heading), body);
  }

  function stat(label, value) {
    return h("div.stat", h("dt", label), h("dd", value));
  }

  /* --------------------------------------------------------------- detail */
  function renderReport(report) {
    const highlights = report.highlights || [];
    const changes = report.changes || [];
    const data = report.data || {};
    const stats = [];

    if (data.competitors_covered != null) {
      stats.push(stat("Competitors", data.competitors_covered));
    }
    if (data.products_tracked != null) {
      stats.push(stat("Products tracked", data.products_tracked));
    }
    if (data.price_moves != null) stats.push(stat("Price changes", data.price_moves));
    if (data.avg_price_delta != null) {
      const delta = Number(data.avg_price_delta);
      stats.push(stat("Average price move", `${delta > 0 ? "+" : ""}${delta}%`));
    }

    return h("article.report",
      h("header.report-head",
        h("div",
          h("h3", report.title || "Report"),
          h("p.report-period",
            `${when(report.period_start)} – ${when(report.period_end || report.generated_at)}`,
            report.generated_at
              ? h("span", " · generated ", ago(report.generated_at))
              : null)),
        button("Close", {
          size: "sm",
          onClick: () => { selectedId = null; render(); },
        })),

      section("Summary",
        h("p.report-summary", report.summary || "No summary was generated for this period.")),

      highlights.length
        ? section("What matters",
            h("ul.highlights", highlights.map((line) => h("li", line))))
        : null,

      stats.length ? section("The numbers", h("dl.stat-grid", stats)) : null,

      changes.length
        ? section(`Changes (${changes.length})`,
            h("ul.change-chips", changes.slice(0, 50).map((change) =>
              h("li.chip", { "data-tone": kindTone(change.kind) },
                kindLabel(change.kind),
                change.new_price != null
                  ? h("span.chip-price", money(change.new_price, change.currency))
                  : null))))
        : h("p.report-muted", "No individual changes were recorded in this period."));
  }

  /* ----------------------------------------------------------------- load */
  async function render() {
    const { shops } = await api.shops();
    const current = shopSelect.value;
    fill(shopSelect, shops.length
      ? shops.map((s) => h("option", { value: String(s.id), selected: current === String(s.id) },
        s.name))
      : h("option", { value: "" }, "No shops yet — add one first"));

    // A failed list must not hide a report the user already has open.
    const [list, detail] = await Promise.all([
      api.reports().catch(() => ({ reports: [] })),
      selectedId ? api.report(selectedId).catch(() => null) : Promise.resolve(null),
    ]);

    fill(listHost, list.reports.length
      ? h("ul.report-list", list.reports.map(reportRow))
      : empty({
          iconName: "report",
          title: "No reports yet",
          message: "Generate one above to see how your competitors moved.",
        }));

    fill(detailHost, detail
      ? renderReport(detail)
      : selectedId
        ? h("p.report-muted", "That report could not be loaded.")
        : h("p.report-muted", "Choose a report to read it."));
  }

  reload = asyncPanel(listHost, render, render);
  return reload;
}
