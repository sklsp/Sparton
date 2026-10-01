// This week: the board of what competitors changed, newest first. One request (`/overview`)
// for the board and the counts, one more for source labels per competitor.

import { api, settleAll, settledValue } from "../api.js";
import { h, fill, errorState } from "../ui.js";
import { t, fmtDate } from "../i18n.js";
import { boardRow } from "../board.js";
import { changeRow, weekLabel, countFlow } from "./board-view.js";

export default function overviewView(host, { navigate }) {
  const load = async () => {
    const [ov, comps] = await settleAll([api.overview(), api.competitors()]);
    if (ov.status === "rejected") throw ov.reason;
    const list = settledValue(comps, { competitors: [] });
    // A quiet week still shows the last moves, so the board is never just an empty frame.
    const earlier = ov.value.shops.length && !ov.value.recent_changes.length
      ? (await api.changes({ days: 60, limit: 8 }).catch(() => ({ changes: [] }))).changes
      : [];
    return { ov: ov.value, competitors: list.competitors || list, earlier };
  };

  const render = ({ ov, competitors, earlier }) => {
    if (!ov.shops.length) return firstRun(navigate);
    const source = new Map(competitors.map((c) => [c.id, c.data_source]));

    const counts = h("p.week-counts",
      countFlow(ov.changes_this_week, "ov.count.changes"),
      countFlow(ov.unacknowledged, "ov.count.unread"),
      countFlow(ov.competitor_count, "ov.count.watched"),
      ov.competitors_failing ? countFlow(ov.competitors_failing, "ov.count.failing", "warn") : null);

    const toRow = (c) => changeRow(c, source.get(c.competitor_id), () => navigate(`product?change=${c.id}`));
    const rows = ov.recent_changes.map(toRow);
    const cols = () => h("div.board-cols", { "aria-hidden": "true" },
      h("span", t("ov.col.competitor")), h("span", t("ov.col.product")),
      h("span.num", t("ov.col.was")), h("span.num", t("ov.col.now")), h("span.num", t("ov.col.change")), h("span"));
    const board = h("section.board.week-board", { "aria-labelledby": "week-board-title" },
      h("div.board-head",
        h("h2.board-title#week-board-title", t("ov.board.title")),
        h("a.board-link", { href: "#/alerts" }, t("ov.board.all"))),
      rows.length
        ? [cols(), h("ol.board-rows", rows)]
        : [quietWeek(ov), earlier.length
            ? [h("h3.board-sub", t("ov.earlier")), cols(), h("ol.board-rows", earlier.map(toRow))]
            : null]);

    const report = ov.latest_report
      ? h("a.report-teaser", { href: "#/reports" },
          h("span.report-teaser-k", t("ov.report.latest")),
          h("strong", ov.latest_report.title || t("ov.report.untitled")),
          h("span.report-teaser-go", t("ov.report.read")))
      : null;

    return [
      h("header.view-head",
        h("h1.view-title", t("ov.title")),
        h("p.view-sub", weekLabel())),
      counts,
      board,
      report,
    ];
  };

  const run = async () => {
    fill(host, h("div.view-head", h("h1.view-title", t("ov.title"))), loadingBoard());
    try {
      fill(host, render(await load()));
    } catch (err) {
      fill(host, h("h1.view-title", t("ov.title")), errorState({ title: t("ov.error"), message: err.message, onRetry: run }));
    }
  };
  run();
}

function quietWeek(ov) {
  const next = ov.shops.map((s) => s.next_crawl_at).filter(Boolean).sort()[0];
  return h("div.board-quiet",
    h("p.board-quiet-title", t("ov.quiet.title")),
    h("p", next ? t("ov.quiet.next", { when: fmtDate(new Date(next), { weekday: "long", day: "numeric", month: "long" }) }) : t("ov.quiet.body")));
}

function firstRun() {
  return h("section.first-run",
    h("h1.view-title", t("ov.first.title")),
    h("p.view-sub", t("ov.first.body")),
    h("a.btn", { href: "#/start", "data-variant": "primary", "data-size": "lg" }, t("ov.first.cta")));
}

/** The board's own loading state: empty tiles, not a spinner. */
export function loadingBoard(n = 5) {
  return h("div.board.is-loading", { "aria-busy": "true", "aria-label": t("ui.loading") },
    h("div.board-head", h("span.board-title", t("ui.loading"))),
    h("ol.board-rows", Array.from({ length: n }, () => boardRow({ a: " ", b: " ", c: "", d: "", e: "" }))));
}

