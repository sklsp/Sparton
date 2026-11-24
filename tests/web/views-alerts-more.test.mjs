// The branch sides of alerts.js that views-alerts.test.mjs does not reach: /competitors and /shops bodies without their list keys (the settledValue fallbacks), and the filtered empty plate when a filter is active but nothing matches. Fixtures mirror the API shapes built in app/api/commerce.py (_shop_dict, _competitor_dict, _change_dict).
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import alertsView from "../../app/web/views/alerts.js";
import { t } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom, Event } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// The shop and its two competitors, as _shop_dict / _competitor_dict build them.
const shop = { id: 1, name: "Bakkerij De Vries", url: "https://devries.nl", domain: "devries.nl", platform: "shopify", category: "bakery", currency: "EUR", crawl_frequency_hours: 24, is_active: true, competitor_count: 2, last_crawled_at: "2026-11-08T09:30:00Z", next_crawl_at: "2026-11-15T09:30:00Z", last_status: "COMPLETED", created_at: "2026-09-01T10:00:00Z" };
const compHtml = { id: 7, shop_id: 1, domain: "bakkerijjansen.nl", name: "Bakkerij Jansen", url: "https://bakkerijjansen.nl", platform: "woocommerce", discovery_method: "search", confidence: 0.92, is_active: true, product_count: 48, last_crawled_at: "2026-11-08T09:30:00Z", last_status: "COMPLETED", data_source: "html", source_label: "Extracted from the page", created_at: "2026-09-02T10:00:00Z" };
const compFeed = { id: 8, shop_id: 1, domain: "broodhuis.nl", name: "Broodhuis", url: "https://broodhuis.nl", platform: "shopify", discovery_method: "search", confidence: 0.97, is_active: true, product_count: 64, last_crawled_at: "2026-11-08T09:35:00Z", last_status: "FAILED", data_source: "api", source_label: "Exact", created_at: "2026-09-02T10:05:00Z" };

// One change, as _change_dict builds it; the filtered re-request comes back empty.
const cut = { id: 301, shop_id: 1, competitor_id: 7, kind: "price_decrease", severity: "high", title: "Zwarte brood down again", summary: "", competitor: "Bakkerij Jansen", competitor_domain: "bakkerijjansen.nl", product: "Zwarte brood", previous_price: 4.5, new_price: 3.95, currency: "EUR", delta: -0.55, delta_pct: -12.2, evidence_url: "https://bakkerijjansen.nl/zwarte-brood", detected_at: "2026-11-13T08:00:00Z", acknowledged_at: null, vs_you: null };
const changesBody = { count: 1, unacknowledged: 1, changes: [cut] };

// Render the alerts view against scripted routes; competitors and shopsBody may omit their list keys.
function renderAlerts({ changes = () => json(changesBody), shops = [shop], competitors = { count: 2, competitors: [compHtml, compFeed] }, shopsBody = null } = {}) {
  resetEnv();
  resetDom();
  resetToasts(); // ui.js caches its toasts host; a fresh document needs a fresh one.
  const host = document.createElement("main");
  document.body.appendChild(host);
  setFetch((href) => {
    if (href.startsWith("/changes?")) return Promise.resolve(changes());
    if (href === "/competitors") return json(competitors);
    if (href === "/shops") return json(shopsBody ?? { shops });
    throw new Error(`unexpected fetch ${href}`);
  });
  alertsView(host, { navigate: () => {} });
  return { host };
}

// Poll one macrotask at a time until the view has settled; never a fixed sleep.
async function waitFor(fn, what) {
  for (let i = 0; i < 100; i++) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error(`timed out waiting for ${what}`);
}

test("a /competitors or /shops body without its list key still renders", async () => {
  const { host } = renderAlerts({
    changes: () => json({ count: 0, unacknowledged: 0, changes: [] }),
    competitors: { count: 0 }, // no "competitors" key at all
    shopsBody: {},             // no "shops" key at all
  });
  await waitFor(() => host.querySelector(".empty-plate"), "the empty state");

  assert.equal(host.querySelector(".week-board"), null);
  const plate = host.querySelector(".empty-plate");
  assert.equal(plate.querySelector("h2").textContent, t("al.empty.noShop")); // no shops key: treated as having none
  assert.ok(plate.querySelector("a.btn"), "the start CTA is offered when there is no shop");
});

test("filtering to nothing shows the filtered empty plate", async () => {
  let first = true;
  const { host } = renderAlerts({
    changes: () => {
      // The initial load has one change; the filtered re-request comes back empty.
      const body = first ? changesBody : { count: 0, unacknowledged: 0, changes: [] };
      first = false;
      return json(body);
    },
  });
  await waitFor(() => host.querySelector(".week-board"), "the alerts to settle");

  const cb = host.querySelector("#al-unread");
  cb.checked = true;
  cb.dispatchEvent(new Event("change"));
  await waitFor(() => host.querySelector(".empty-plate"), "the filtered empty state");

  const plate = host.querySelector(".empty-plate");
  assert.equal(plate.querySelector("h2").textContent, t("al.empty.filtered"));
  assert.equal(plate.querySelector("p").textContent, t("al.empty.filteredBody"));
  // A shop exists: no start CTA; nothing unread left: no mark-all button.
  assert.equal(plate.querySelector("a.btn"), null);
  assert.equal(host.querySelectorAll(".al-toolbar button").length, 0);
});
