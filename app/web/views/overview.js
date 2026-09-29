// Overview — what happened, and what to do next.
//
// The whole screen is driven by one `GET /overview` round trip. That is a
// deliberate constraint from the API side ("everything the dashboard needs in
// one round trip") and it should not be undone by convenience fetches here.
//
// The ordering is the argument: headline numbers, then the thing that needs a
// decision (unread alerts), then the shops you are watching. A customer who
// only reads the top third still learns something true.

import { api } from "../api.js";
import { h, fill, asyncPanel, empty, skeletonMetrics, button, ago, num, badge } from "../ui.js";
import { money, kindLabel, kindTone } from "./shared.js";

function greeting(user) {
  const hour = new Date().getHours();
  const part = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";
  const name = (user?.email || "").split("@")[0];
  return name ? `${part}, ${name}` : part;
}

export default function overviewView(host, { navigate, state }) {
  const metricsHost = h("div");
  const alertsHost = h("div.stack");
  const shopsHost = h("div.stack");
  let reload = () => {};

  host.append(
    h("div.page-head",
      h("div",
        h("h1", greeting(state?.user)),
        h("p", "What your competitors did in the last seven days."))),
    metricsHost,
    h("section.ov-section",
      h("div.ov-section-head",
        h("h2.ov-section-title", "Needs your attention"),
        button("All alerts", { size: "sm", onClick: () => navigate("alerts") })),
      alertsHost),
    h("section.ov-section",
      h("div.ov-section-head",
        h("h2.ov-section-title", "Shops you are watching"),
        button("Manage shops", { size: "sm", onClick: () => navigate("shops") })),
      shopsHost));

  /* -------------------------------------------------------------- metrics */
  function metrics(data) {
    const failing = data.competitors_failing;
    return h("div.metrics",
      h("div.metric", { "data-tone": "info" },
        h("p.metric-label", "Competitors tracked"),
        h("p.metric-value", num(data.competitor_count)),
        h("p.metric-sub", `${data.competitors_healthy} crawled cleanly`)),
      h("div.metric", { "data-tone": "neutral" },
        h("p.metric-label", "Changes this week"),
        h("p.metric-value", num(data.changes_this_week)),
        h("p.metric-sub", data.changes_this_week
          ? "price, stock and assortment"
          : "nothing moved")),
      h("div.metric", {
        "data-tone": data.unacknowledged ? "warning" : "neutral",
      },
        h("p.metric-label", "Unread alerts"),
        h("p.metric-value", num(data.unacknowledged)),
        h("p.metric-sub", data.unacknowledged ? "waiting on you" : "all caught up")),
      h("div.metric", { "data-tone": failing ? "danger" : "neutral" },
        h("p.metric-label", "Crawl problems"),
        h("p.metric-value", num(failing)),
        h("p.metric-sub", failing
          ? "blocked or failing — check robots.txt"
          : "none")));
  }

  /* --------------------------------------------------------------- alerts */
  function changeRow(change) {
    return h("li.ov-change", { "data-tone": kindTone(change.kind) },
      h("div.ov-change-main",
        h("p.ov-change-title", change.title || kindLabel(change.kind)),
        h("p.ov-change-meta",
          change.competitor ? h("span", change.competitor, " · ") : null,
          change.product ? h("span", change.product, " · ") : null,
          h("time", { datetime: change.detected_at || "" }, ago(change.detected_at)))),
      h("div.ov-change-side",
        change.new_price != null
          ? h("span.ov-change-price", money(change.new_price, change.currency))
          : badge(kindLabel(change.kind), kindTone(change.kind)),
        change.evidence_url
          ? h("a.ov-evidence", {
              href: change.evidence_url, target: "_blank", rel: "noopener noreferrer",
            }, "Evidence ↗")
          : null));
  }

  function renderAlerts(data) {
    const changes = data.recent_changes || [];
    if (!changes.length) {
      fill(alertsHost, empty({
        iconName: "check",
        title: data.competitor_count
          ? "Nothing has moved this week"
          : "Nothing to watch yet",
        message: data.competitor_count
          ? "No competitor changed a price, added a product or ran out of "
            + "stock in the last seven days."
          : "Add a shop and a competitor, and this is where their changes "
            + "will appear.",
        action: data.competitor_count
          ? null
          : { label: "Add your first shop", onClick: () => navigate("shops") },
      }));
      return;
    }
    fill(alertsHost, h("ul.ov-change-list", changes.map(changeRow)));
  }

  /* ---------------------------------------------------------------- shops */
  function renderShops(data) {
    const shops = data.shops || [];
    if (!shops.length) {
      fill(shopsHost, empty({
        iconName: "shop",
        title: "No shops yet",
        message: "Add your shop and we will start watching the competitors you choose.",
        action: { label: "Add a shop", onClick: () => navigate("shops") },
      }));
      return;
    }
    fill(shopsHost,
      h("div.ov-shop-grid",
        shops.map((shop) =>
          h("a.ov-shop", { href: "#/shops" },
            h("p.ov-shop-name", shop.name),
            h("p.ov-shop-meta", shop.domain),
            h("p.ov-shop-crawl", shop.last_crawled_at
              ? `Last crawl ${ago(shop.last_crawled_at)}`
              : "Never crawled")))));
  }

  /* ----------------------------------------------------------------- load */
  async function render() {
    const data = await api.overview();
    fill(metricsHost, metrics(data));
    renderAlerts(data);
    renderShops(data);
  }

  reload = asyncPanel(metricsHost, render, render, {
    loading: () => skeletonMetrics(4),
  });
  return reload;
}
