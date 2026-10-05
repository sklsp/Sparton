// This week: the board of what competitors changed, newest first. One request (`/overview`)
// for the board and the counts, one more for source labels per competitor.

import { api, settleAll, settledValue } from "../api.js";
import { h, fill, errorState } from "../ui.js";
import { t, fmtDate } from "../i18n.js";
import { boardRow } from "../board.js";
import { changeRow, weekLabel, boardCounts, boardLegend } from "./board-view.js";

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

    const counts = boardCounts([
      [ov.changes_this_week, "ov.count.changes"],
      [ov.unacknowledged, "ov.count.unread"],
      [ov.competitor_count, "ov.count.watched"],
      ov.competitors_failing ? [ov.competitors_failing, "ov.count.failing", "warn"] : null,
    ]);

    const toRow = (c) => changeRow(c, source.get(c.competitor_id), () => navigate(`product?change=${c.id}`));
    const rows = ov.recent_changes.map(toRow);
    const cols = () => h("div.board-cols", { "aria-hidden": "true" },
      h("span", t("ov.col.competitor")), h("span", t("ov.col.product")),
      h("span.num", t("ov.col.was")), h("span.num", t("ov.col.now")), h("span.num", t("ov.col.change")), h("span"));
    const board = h("section.board.week-board", { "aria-labelledby": "week-board-title" },
      h("div.board-head",
        h("h2.board-title#week-board-title", t("ov.board.title")),
        h("a.board-link", { href: "#/alerts" }, t("ov.board.all"))),
      counts,
      rows.length
        ? [cols(), h("ol.board-rows", rows), boardLegend(ov.recent_changes, source)]
        : [quietWeek(ov, competitors), earlier.length
            ? [h("h3.board-sub", t("ov.earlier")), cols(), h("ol.board-rows", earlier.map(toRow)), boardLegend(earlier, source)]
            : null]);

    const report = ov.latest_report
      ? h("a.report-teaser", { href: "#/reports" },
          h("span.report-teaser-k", t("ov.report.latest")),
          h("strong", reportTitle(ov.latest_report)),
          h("span.report-teaser-go", t("ov.report.read")))
      : null;

    return [
      h("header.view-head",
        h("h1.view-title", t("ov.title")),
        h("p.view-sub", weekLabel())),
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

/** The server writes report titles in English; build ours from the report's own facts. */
export function reportTitle(r) {
  const shop = r.facts?.shop?.name;
  const end = r.period_end ? fmtDate(new Date(r.period_end), { day: "numeric", month: "short" }) : "";
  return shop ? `${t("rp.title", { shop })}${end ? ` · ${end}` : ""}` : t("ov.report.untitled");
}

/** A quiet week is one board line, so earlier moves still fill the first screen. */
function quietWeek(ov, competitors) {
  const long = { weekday: "long", day: "numeric", month: "long" };
  const next = ov.shops.map((s) => s.next_crawl_at).filter(Boolean).sort()[0];
  const last = competitors.map((c) => c.last_crawled_at).filter(Boolean).sort().at(-1);
  // "Changes appear after the second check" is first-run copy; a watched account that saw
  // nothing move is told when it last looked, so a quiet week never reads as a broken one.
  const detail = last
    ? t("ov.quiet.steady", { when: fmtDate(new Date(last), long) })
    : next ? t("ov.quiet.next", { when: fmtDate(new Date(next), long) }) : t("ov.quiet.body");
  return h("p.board-quiet", h("strong", t("ov.quiet.title")), " ", detail);
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

