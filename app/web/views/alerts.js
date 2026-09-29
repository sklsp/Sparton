// Alerts — the in-app inbox. This is the screen a customer opens daily.
//
// Three rules this view keeps:
//  1. Every alert links to the product page the number was read from. If we
//     cannot show the evidence, we should not be making the claim.
//  2. An empty inbox says so plainly. A quiet week is information.
//  3. Nothing is deleted, only acknowledged, so the history stays auditable.

import { api, settleAll, settledValue } from "../api.js";
import { h, fill, asyncPanel, empty, toast, ago, badge, button } from "../ui.js";
import { money, kindLabel, kindTone } from "./shared.js";

const KINDS = [
  ["", "All changes"],
  ["price_decrease", "Price cuts"],
  ["price_increase", "Price rises"],
  ["new_product", "New products"],
  ["removed_product", "Delisted"],
  ["out_of_stock", "Out of stock"],
  ["back_in_stock", "Back in stock"],
];

const filters = { kind: "", shopId: null, unacknowledgedOnly: false };

export default function alertsView(host, { navigate }) {
  const listHost = h("div.stack");

  const kindSelect = h(
    "select.input",
    {
      "aria-label": "Filter by change type",
      onchange: (e) => { filters.kind = e.target.value; reload(); },
    },
    KINDS.map(([value, label]) =>
      h("option", { value, selected: filters.kind === value }, label)),
  );

  const shopSelect = h(
    "select.input",
    {
      "aria-label": "Filter by shop",
      onchange: (e) => {
        filters.shopId = e.target.value ? Number(e.target.value) : null;
        reload();
      },
    },
    h("option", { value: "" }, "All shops"),
  );

  const unreadInput = h("input", {
    type: "checkbox",
    onchange: (e) => { filters.unacknowledgedOnly = e.target.checked; reload(); },
  });

  fill(host,
    h("div.toolbar",
      h("h2.view-title", "Alerts"),
      h("div.toolbar-controls",
        kindSelect, shopSelect,
        h("label.check", unreadInput, "Unread only"),
        button("Refresh", { onClick: () => reload() }))),
    listHost);

  /* ------------------------------------------------------------- one alert */
  function changeCard(change) {
    const tone = kindTone(change.kind);
    const hasPrices = change.previous_price != null && change.new_price != null;

    return h("article.alert", {
      "data-tone": tone,
      "data-unread": change.acknowledged_at ? "false" : "true",
    },
      h("div.alert-head",
        h("span.alert-kind", kindLabel(change.kind)),
        change.delta_pct != null
          ? badge(`${change.delta_pct > 0 ? "+" : ""}${change.delta_pct.toFixed(0)}%`, tone)
          : null,
        h("time.alert-time", { datetime: change.detected_at || "" }, ago(change.detected_at))),
      h("p.alert-title", change.title),
      change.summary ? h("p.alert-summary", change.summary) : null,
      h("div.alert-foot",
        h("div.alert-prices",
          change.previous_price != null
            ? h("span.price-was", money(change.previous_price, change.currency))
            : null,
          hasPrices ? h("span.price-arrow", { "aria-hidden": "true" }, "→") : null,
          change.new_price != null
            ? h("span.price-now", money(change.new_price, change.currency))
            : null),
        change.evidence_url
          ? h("a.evidence", {
              href: change.evidence_url,
              target: "_blank",
              rel: "noopener noreferrer",
            }, "Evidence", h("span.evidence-icon", { "aria-hidden": "true" }, "↗"))
          : h("span.evidence.evidence-missing", "No evidence link"),
        !change.acknowledged_at
          ? button("Mark read", {
              size: "sm",
              onClick: (event) => acknowledge(change.id, event.currentTarget),
            })
          : null));
  }

  async function acknowledge(id, button) {
    button.disabled = true;
    try {
      await api.acknowledgeChange(id);
      toast("Marked as read", "success");
      reload();
    } catch (error) {
      button.disabled = false;
      toast(error.message, "danger");
    }
  }

  /* --------------------------------------------------------------- render */
  async function render() {
    const [changes, shopList] = await settleAll([
      api.changes({
        kind: filters.kind || undefined,
        shopId: filters.shopId || undefined,
        unacknowledgedOnly: filters.unacknowledgedOnly,
        days: 30,
        limit: 100,
      }),
      api.shops(),
    ]);

    // Rebuild the shop options, keeping the current selection.
    const shops = settledValue(shopList, { shops: [] }).shops || [];
    fill(shopSelect,
      h("option", { value: "" }, "All shops"),
      shops.map((s) => h("option", {
        value: String(s.id),
        selected: filters.shopId === s.id,
      }, s.name)));

    const data = settledValue(changes, null);
    if (!data) throw new Error("Could not load alerts");

    const filtered = filters.kind || filters.unacknowledgedOnly || filters.shopId;
    if (!data.changes.length) {
      fill(listHost, empty({
        iconName: filtered ? "search" : "info",
        title: filtered ? "Nothing matches that filter" : "No alerts yet",
        message: filtered
          ? "Try widening the filter."
          : "We alert you the moment a competitor changes a price, adds a "
            + "product, or goes out of stock. The first crawl of a new "
            + "competitor sets a baseline, so real changes appear from the "
            + "second crawl onward.",
        action: filtered
          ? { label: "Clear filters", onClick: clearFilters }
          : { label: "Add a shop", onClick: () => navigate("shops") },
      }));
      return;
    }

    fill(listHost,
      h("p.list-meta",
        `${data.count} alert${data.count === 1 ? "" : "s"}`,
        data.unacknowledged ? ` · ${data.unacknowledged} unread` : ""),
      h("div.alert-list", data.changes.map(changeCard)));
  }

  function clearFilters() {
    filters.kind = "";
    filters.shopId = null;
    filters.unacknowledgedOnly = false;
    kindSelect.value = "";
    shopSelect.value = "";
    unreadInput.checked = false;
    reload();
  }

  const reload = asyncPanel(listHost, render, render);
  return reload;
}
