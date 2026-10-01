// Every change, newest first, ruled by week: the same time axis as This week and the reports.

import { api, settleAll, settledValue } from "../api.js";
import { h, fill, button, toast, errorState } from "../ui.js";
import { t } from "../i18n.js";
import { changeRow, isoWeek } from "./board-view.js";
import { loadingBoard } from "./overview.js";

const KINDS = ["price_decrease", "price_increase", "out_of_stock", "back_in_stock", "new_product", "removed_product"];

export default function alertsView(host, { navigate }) {
  const filters = { unread: false, kind: "" };

  const run = async () => {
    fill(host, head(), loadingBoard(6));
    try {
      const [ch, comps, sh] = await settleAll([
        api.changes({ days: 90, limit: 200, kind: filters.kind || undefined, unacknowledgedOnly: filters.unread }),
        api.competitors(),
        api.shops(),
      ]);
      if (ch.status === "rejected") throw ch.reason;
      const { changes, unacknowledged } = ch.value;
      const source = new Map((settledValue(comps, { competitors: [] }).competitors || []).map((c) => [c.id, c.data_source]));
      const hasShops = (settledValue(sh, { shops: [] }).shops || []).length > 0;

      const markAll = unacknowledged
        ? button(t("al.markAll", { n: unacknowledged }), { size: "sm", onClick: async () => {
            markAll.dataset.loading = "true";
            try {
              const unread = changes.filter((c) => !c.acknowledged_at);
              for (const c of unread) await api.acknowledgeChange(c.id);
              toast(t("al.marked"), "success");
              run();
            } catch (err) { toast(err.message, "danger"); delete markAll.dataset.loading; }
          } })
        : null;

      fill(host, head(), toolbar(markAll), changes.length
        ? weeks(changes).map(([week, rows]) => h("section.board.week-board", { "aria-label": t("week.label", { n: week }) },
            h("div.board-head", h("h2.board-title", t("week.label", { n: week }))),
            h("ol.board-rows", rows.map((c) => changeRow(c, source.get(c.competitor_id), () => navigate(`product?change=${c.id}`))))))
        : empty(hasShops));
    } catch (err) {
      fill(host, head(), errorState({ title: t("al.error"), message: err.message, onRetry: run }));
    }
  };

  function toolbar(markAll) {
    const unread = h("input#al-unread", { type: "checkbox", checked: filters.unread, onchange: () => { filters.unread = unread.checked; run(); } });
    const kind = h("select.input#al-kind", { onchange: () => { filters.kind = kind.value; run(); } },
      h("option", { value: "" }, t("al.kind.all")),
      KINDS.map((k) => h("option", { value: k, selected: filters.kind === k }, t(`al.kind.${k}`))));
    return h("div.al-toolbar",
      h("label.al-check", { for: "al-unread" }, unread, t("al.unreadOnly")),
      h("label.al-select", { for: "al-kind" }, h("span", t("al.kind")), kind),
      markAll);
  }

  function empty(hasShops) {
    const filtered = filters.unread || filters.kind;
    return h("section.plate.empty-plate",
      h("h2", t(filtered ? "al.empty.filtered" : hasShops ? "al.empty.title" : "al.empty.noShop")),
      h("p", t(filtered ? "al.empty.filteredBody" : hasShops ? "al.empty.body" : "al.empty.noShopBody")),
      !hasShops ? h("a.btn", { href: "#/start", "data-variant": "primary" }, t("ov.first.cta")) : null);
  }

  run();
}

const head = () => h("header.view-head", h("h1.view-title", t("nav.alerts")), h("p.view-sub", t("al.sub")));

/** Changes grouped by ISO week, newest week first. */
function weeks(changes) {
  const groups = new Map();
  for (const c of changes) {
    const w = isoWeek(new Date(c.detected_at));
    if (!groups.has(w)) groups.set(w, []);
    groups.get(w).push(c);
  }
  return [...groups];
}
