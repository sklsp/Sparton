// Catalog — products, inventory and sales analytics.

import { api } from "../api.js";
import {
  h, fill, icon, metric, button, badge, statusBadge, skeletonMetrics, empty, errorState,
  dialog, num, money, title, asyncPanel, skeleton,
} from "../ui.js";

export default function catalog(view) {
  view.append(
    h("div.page-head",
      h("div",
        h("h1", "Catalog"),
        h("p", "Products, stock levels and sales performance across the workspace."))));

  const statsHost = h("div");
  view.append(statsHost);
  asyncPanel(statsHost, () => api.analytics(), renderAnalytics, { loading: () => skeletonMetrics(4) });

  // --- products
  const productsHost = h("section.card");
  const productsBody = h("div.card-body.flush");
  const search = h("input.input", { type: "search", placeholder: "Search products…", "aria-label": "Search products" });

  productsHost.append(
    h("header.card-head",
      h("div", h("h2", "Products"), h("div.sub", "Server-side search across the catalog")),
      h("div.card-head-actions", h("div.search", { style: { minWidth: "220px" } }, icon("search", 15), search))),
    productsBody);
  view.append(productsHost);

  let term = "";
  const reload = asyncPanel(productsBody, () => api.products(term || undefined, 100), renderProducts);

  // Debounced so a fast typist does not fire a request per keystroke.
  let debounce;
  search.addEventListener("input", () => {
    clearTimeout(debounce);
    debounce = setTimeout(() => { term = search.value.trim(); reload(); }, 280);
  });

  return () => clearTimeout(debounce);
}

/* ------------------------------------------------------------- analytics */

function renderAnalytics(data) {
  const catalogStats = data.catalog || {};
  const inventory = data.inventory || {};
  const sales = data.sales || {};
  const aov = sales.order_count_total ? sales.total_revenue / sales.order_count_total : 0;

  return h("div.grid.grid-metrics",
    metric({ label: "Products", value: num(catalogStats.product_count), iconName: "catalog",
      sub: "tracked in this workspace" }),
    metric({ label: "Revenue", value: money(sales.total_revenue), iconName: "chart",
      sub: h("span", h("b", money(aov)), " average order") }),
    metric({ label: "Orders", value: num(sales.order_count_total), iconName: "store",
      sub: h("span", h("b", num(sales.orders_last_30_days)), " in the last 30 days") }),
    metric({ label: "Low stock", value: num(inventory.low_stock_count), iconName: "alert",
      tone: inventory.low_stock_count ? "warning" : undefined,
      sub: inventory.low_stock_count ? "at or below reorder point" : "every SKU is above its reorder point" }));
}

/* -------------------------------------------------------------- products */

function renderProducts(data) {
  const products = data.products || [];
  if (!products.length) {
    return empty({
      iconName: "catalog",
      title: "No products found",
      message: "Nothing matches this search, or the catalog has not been populated yet.",
    });
  }

  let sortKey = "id";
  let sortDir = 1;
  const body = h("tbody");

  const paint = () => {
    const sorted = [...products].sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      if (typeof av === "string" || typeof bv === "string") {
        return String(av ?? "").localeCompare(String(bv ?? "")) * sortDir;
      }
      return ((av ?? 0) - (bv ?? 0)) * sortDir;
    });
    fill(body, sorted.map((product) => {
      const stock = product.inventory ?? 0;
      return h("tr", { style: { cursor: "pointer" }, tabindex: "0",
        onclick: () => showProduct(product.id),
        onkeydown: (e) => { if (e.key === "Enter") showProduct(product.id); } },
        h("td.mono", product.sku),
        h("td", h("div.truncate", { title: product.title }, product.title)),
        h("td", product.category ? badge(product.category, "neutral") : h("span", { style: { color: "var(--text-3)" } }, "—")),
        h("td", statusBadge(product.status)),
        h("td.num", money(product.price)),
        h("td.num", { style: { color: stock === 0 ? "var(--danger)" : stock < 10 ? "var(--warning)" : undefined } },
          num(stock)));
    }));
  };

  const th = (label, key, numeric) => {
    const cell = h(numeric ? "th.num" : "th", { "data-sortable": "", tabindex: "0",
      "aria-sort": key === sortKey ? (sortDir === 1 ? "ascending" : "descending") : "none" },
      label, h("span.sort-caret", "▾"));
    const toggle = () => {
      sortDir = key === sortKey ? -sortDir : 1;
      sortKey = key;
      for (const other of cell.parentElement.children) other.setAttribute("aria-sort", "none");
      cell.setAttribute("aria-sort", sortDir === 1 ? "ascending" : "descending");
      paint();
    };
    cell.onclick = toggle;
    cell.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } };
    return cell;
  };

  const table = h("table",
    h("thead", h("tr",
      th("SKU", "sku"), th("Title", "title"), th("Category", "category"),
      h("th", "Status"), th("Price", "price", true), th("Stock", "inventory", true))),
    body);
  paint();

  return h("div",
    h("div.table-wrap", table),
    h("footer.card-foot", `${num(products.length)} product${products.length === 1 ? "" : "s"}`,
      categoryBreakdown(products)));
}

/** Compact distribution bar — the only chart the catalog actually needs. */
function categoryBreakdown(products) {
  const counts = new Map();
  for (const product of products) {
    const key = product.category || "uncategorised";
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  if (counts.size < 2) return null;
  const top = [...counts].sort((a, b) => b[1] - a[1]).slice(0, 5);
  return h("div", { style: { marginLeft: "auto", display: "flex", gap: "12px", flexWrap: "wrap" } },
    top.map(([name, count]) =>
      h("span", { style: { display: "flex", alignItems: "center", gap: "5px" } },
        h("span.dot", { style: { background: "var(--primary)" } }), `${name} ${count}`)));
}

async function showProduct(id) {
  const el = dialog({ title: "Product", body: skeleton(5), actions: [] });
  const body = el.querySelector(".dialog-body");
  try {
    const product = await api.product(id);
    fill(body,
      h("div",
        h("h3", { style: { font: "var(--t-title)", marginBottom: "6px" } }, product.title),
        h("div.chips", h("span.chip", product.sku), statusBadge(product.status),
          product.category ? badge(product.category, "neutral") : null)),
      h("p", { style: { color: "var(--text-2)" } }, product.description || "No description yet. A good candidate for the agent to rewrite."),
      h("dl.kv",
        h("dt", "Price"), h("dd", money(product.price)),
        h("dt", "Inventory"), h("dd", num(product.inventory)),
        h("dt", "Category"), h("dd", title(product.category || "uncategorised")),
        h("dt", "Product ID"), h("dd", `#${product.id}`)));
  } catch (err) {
    fill(body, errorState({ message: err.message }));
  }
  el.querySelector(".dialog-foot")?.remove();
  el.append(h("footer.dialog-foot", button("Close", { variant: "ghost", onClick: () => el.close() })));
}
