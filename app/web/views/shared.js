// Helpers shared by the product views. Small on purpose: anything used by one
// view only lives in that view.
//
// This file deliberately re-exports nothing that `ui.js` already provides.
// `ago`, `when`, `empty`, `badge`, `card`, `metric` and friends stay in one
// place; what is here is only what is *specific to the product domain*.

import { ago, when } from "../ui.js";

export { ago, when };

const SYMBOLS = { EUR: "€", USD: "$", GBP: "£" };

/** `27.5, "EUR"` -> `€27.50`. Null renders as an em dash, never "null". */
export function money(value, currency = "EUR") {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const symbol = SYMBOLS[(currency || "").toUpperCase()] || "";
  const formatted = Number(value).toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return symbol ? `${symbol}${formatted}` : `${formatted} ${String(currency).toUpperCase()}`;
}

/** `3400` -> "€3,400" for headline figures, where cents are noise. */
export function moneyShort(value, currency = "EUR") {
  if (value == null || Number.isNaN(Number(value))) return "—";
  const symbol = SYMBOLS[(currency || "").toUpperCase()] || "";
  return `${symbol}${Math.round(Number(value)).toLocaleString()}`;
}

const KIND_LABELS = {
  price_decrease: "Price cut",
  price_increase: "Price rise",
  new_product: "New product",
  removed_product: "Delisted",
  out_of_stock: "Out of stock",
  back_in_stock: "Back in stock",
};

export function kindLabel(kind) {
  return KIND_LABELS[kind] || kind || "Change";
}

/** Maps a change kind onto the existing badge tone vocabulary. */
export function kindTone(kind) {
  if (kind === "price_decrease") return "danger";
  if (kind === "out_of_stock") return "warning";
  if (kind === "price_increase") return "info";
  return "neutral";
}

/** Status a competitor is in, mapped to a badge tone. */
export function crawlTone(status) {
  if (status === "COMPLETED") return "success";
  if (status === "PARTIAL") return "warning";
  if (status === "FAILED" || status === "BLOCKED") return "danger";
  return "neutral";
}

/** Platform a shop runs on, in words. */
export function platformLabel(platform) {
  const names = {
    shopify: "Shopify",
    woocommerce: "WooCommerce",
    bol: "Bol.com",
    magento: "Magento",
    bigcommerce: "BigCommerce",
    prestashop: "PrestaShop",
    unknown: "Unknown",
  };
  return names[platform] || platform || "Unknown";
}

/** Is this a plan-limit refusal rather than a genuine error? */
export function isPlanLimit(error) {
  return error?.status === 402;
}

/** The message a plan limit should show, falling back to the server's. */
export function planLimitMessage(error) {
  const detail = error?.detail;
  if (detail && typeof detail === "object") {
    return detail.message || "You have reached the limit on your plan.";
  }
  return typeof detail === "string" ? detail : "You have reached the limit on your plan.";
}

