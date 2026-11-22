// The shared view helpers: money and moneyShort with their symbol and code-suffix fallbacks,
// the change-kind labels and tones, crawl-status tones, platform names, and the plan-limit
// error helpers. Pure functions, so no fetch routing is needed; env and dom are imported
// first because shared.js pulls in ui.js.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake (ui.js may touch document at import).
import * as shared from "../../app/web/views/shared.js";

test("money renders known currencies with their symbol", () => {
  assert.equal(shared.money(27.5), "\u20ac27.50"); // docstring example, EUR default
  assert.equal(shared.money(1234.5, "USD"), "$1,234.50");
  assert.equal(shared.money(99, "GBP"), "\u00a399.00");
});

test("money renders null and NaN as an em dash", () => {
  assert.equal(shared.money(null), "\u2014");
  assert.equal(shared.money(undefined), "\u2014");
  assert.equal(shared.money(Number.NaN), "\u2014");
  assert.equal(shared.money("abc"), "\u2014"); // Number("abc") is NaN
});

test("money falls back to a code suffix for unknown currencies", () => {
  assert.equal(shared.money(100, "SEK"), "100.00 SEK");
  assert.equal(shared.money(100, "sek"), "100.00 SEK"); // the code is uppercased in the suffix
  assert.equal(shared.money(5, ""), "5.00 "); // empty currency: no symbol and an empty code suffix
});

test("moneyShort rounds to whole units for headline figures", () => {
  assert.equal(shared.moneyShort(3400), "\u20ac3,400"); // docstring example
  assert.equal(shared.moneyShort(1250.75, "USD"), "$1,251");
  assert.equal(shared.moneyShort(5, ""), "5"); // empty currency: moneyShort has no suffix branch
});

test("moneyShort renders null and NaN as an em dash", () => {
  assert.equal(shared.moneyShort(null), "\u2014");
  assert.equal(shared.moneyShort(Number.NaN), "\u2014");
});

test("moneyShort drops the symbol for unknown currencies", () => {
  assert.equal(shared.moneyShort(999, "SEK"), "999");
});

test("kindLabel names every change kind and passes others through", () => {
  assert.equal(shared.kindLabel("price_decrease"), "Price cut");
  assert.equal(shared.kindLabel("out_of_stock"), "Out of stock");
  assert.equal(shared.kindLabel("price_increase"), "Price rise");
  assert.equal(shared.kindLabel("new_product"), "New product");
  assert.equal(shared.kindLabel("removed_product"), "Delisted");
  assert.equal(shared.kindLabel("back_in_stock"), "Back in stock");
  assert.equal(shared.kindLabel("mystery_kind"), "mystery_kind"); // unknown kinds pass through
  assert.equal(shared.kindLabel(null), "Change");
  assert.equal(shared.kindLabel(undefined), "Change");
});

test("kindTone maps kinds onto badge tones", () => {
  assert.equal(shared.kindTone("price_decrease"), "danger");
  assert.equal(shared.kindTone("out_of_stock"), "warning");
  assert.equal(shared.kindTone("price_increase"), "info");
  assert.equal(shared.kindTone("new_product"), "neutral");
  assert.equal(shared.kindTone("removed_product"), "neutral");
  assert.equal(shared.kindTone(null), "neutral");
});

test("crawlTone maps competitor statuses onto badge tones", () => {
  assert.equal(shared.crawlTone("COMPLETED"), "success");
  assert.equal(shared.crawlTone("PARTIAL"), "warning");
  assert.equal(shared.crawlTone("FAILED"), "danger");
  assert.equal(shared.crawlTone("BLOCKED"), "danger");
  assert.equal(shared.crawlTone("PENDING"), "neutral"); // anything else stays neutral
  assert.equal(shared.crawlTone(null), "neutral");
});

test("platformLabel names known platforms and passes others through", () => {
  assert.equal(shared.platformLabel("shopify"), "Shopify");
  assert.equal(shared.platformLabel("woocommerce"), "WooCommerce");
  assert.equal(shared.platformLabel("bol"), "Bol.com");
  assert.equal(shared.platformLabel("magento"), "Magento");
  assert.equal(shared.platformLabel("bigcommerce"), "BigCommerce");
  assert.equal(shared.platformLabel("prestashop"), "PrestaShop");
  assert.equal(shared.platformLabel("unknown"), "Unknown"); // the literal key in the map
  assert.equal(shared.platformLabel("custom-cms"), "custom-cms"); // anything else passes through
  assert.equal(shared.platformLabel(null), "Unknown");
});

test("isPlanLimit only recognises a 402 status", () => {
  assert.equal(shared.isPlanLimit({ status: 402 }), true);
  assert.equal(shared.isPlanLimit({ status: 403 }), false);
  assert.equal(shared.isPlanLimit({}), false); // no status at all
  assert.equal(shared.isPlanLimit(null), false);
  assert.equal(shared.isPlanLimit(undefined), false);
});

test("planLimitMessage prefers the server's message and falls back to a default", () => {
  assert.equal(shared.planLimitMessage({ detail: { message: "Add a rival on Business." } }), "Add a rival on Business.");
  assert.equal(shared.planLimitMessage({ detail: {} }), "You have reached the limit on your plan."); // object without message
  assert.equal(shared.planLimitMessage({ detail: "Server says no." }), "Server says no."); // plain-string detail
  assert.equal(shared.planLimitMessage({}), "You have reached the limit on your plan."); // no detail at all
  assert.equal(shared.planLimitMessage(null), "You have reached the limit on your plan.");
});
