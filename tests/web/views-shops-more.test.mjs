// Branch coverage for shops.js's name fallbacks: the paths that only fire when a competitor row has no display name:
// the "name || domain" used in both the row heading and the remove dialog title, plus the bare-array shape of the competitors answer.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import shopsView from "../../app/web/views/shops.js";
import { t } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom } from "./dom.mjs";

const json = (body) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });

// One watched shop and two rivals: one with a display name, one without.
const shop = { id: 1, domain: "studio.example", name: "Studio", url: "https://studio.example", platform: "Shopify", crawl_frequency_hours: 24, next_crawl_at: null };
// No `name` key anywhere in the second rival; the view must fall back to its domain.
const rivalNamed = { id: 11, shop_id: 1, name: "Broodhuis", domain: "broodhuis.example", url: "https://broodhuis.example", platform: "WooCommerce", data_source: "feed", product_count: 64, last_crawled_at: "2026-11-10T09:30:00Z", last_status: "COMPLETED" };
const rivalNameless = { id: 12, shop_id: 1, domain: "no-name.example", url: "https://no-name.example", platform: "Shopify", data_source: "html", product_count: 37, last_crawled_at: "2026-11-13T09:30:00Z", last_status: "COMPLETED" };

function renderShops(comps) {
  resetEnv(); resetDom(); resetToasts();
  const host = document.createElement("main");
  document.body.appendChild(host);
  setFetch((href) => {
    if (href === "/shops") return json({ count: 1, shops: [shop] });
    if (href === "/competitors") return json(comps); // sometimes a bare array, see below
    throw new Error(`unexpected fetch ${href}`);
  });
  shopsView(host);
  return host;
}

// Poll one macrotask at a time until the view has settled; never a fixed sleep.
async function waitFor(fn, what) {
  for (let i = 0; i < 200; i += 1) {
    if (fn()) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.fail(`timed out waiting for ${what}`);
}

test("a competitor without a name shows its domain in the row", async () => {
  const host = renderShops([rivalNamed, rivalNameless]); // bare array: no "competitors" key at all
  await waitFor(() => host.querySelectorAll(".comp-row").length === 2, "both competitor rows");
  const [namedRow, namelessRow] = [...host.querySelectorAll("li.comp-row")];
  assert.equal(namedRow.querySelector("strong.comp-name").textContent, "Broodhuis");
  assert.equal(namelessRow.querySelector("strong.comp-name").textContent, "no-name.example"); // fell back to the domain
});

test("the remove dialog titles a nameless competitor by its domain", async () => {
  const host = renderShops({ count: 2, competitors: [rivalNamed, rivalNameless] });
  await waitFor(() => host.querySelectorAll(".comp-row").length === 2, "both competitor rows");
  const namelessRow = [...host.querySelectorAll("li.comp-row")].find((row) => row.querySelector("strong.comp-name").textContent === "no-name.example");
  const remove = [...namelessRow.querySelectorAll("button")].find((b) => b.textContent === t("sh.remove"));
  remove.click();
  await waitFor(() => document.querySelector("#dlg-title"), "the confirm dialog to open");
  assert.equal(document.querySelector("#dlg-title").textContent, t("sh.removeTitle", { name: rivalNameless.domain })); // domain in the title
  const cancel = [...document.querySelectorAll(".dialog-foot button")].find((b) => b.textContent === t("ui.cancel"));
  cancel.click();
});
