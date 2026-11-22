// catalog.js on the DOM fake: the four stat cards from /analytics/summary, the product table
// with its debounced server-side search, the empty plate and the per-product dialog from
// /products/{id}. Fixtures mirror the API shapes built in app/api/ecommerce.py (_product_dict,
// analytics_summary). These tests found BUG #1: api.js had no analytics(), products() or
// product() methods, so the whole view failed; the methods exist now.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import catalogView from "../../app/web/views/catalog.js";
import { t } from "../../app/web/i18n.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom, Event } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// Three products across two categories covering every stock and status state.
const productSourdough = { id: 1, sku: "BRD-001", title: "Sourdough Loaf", description: "Naturally leavened wheat bread.", price: 6.95, category: "bread", status: "active", inventory: 42 };
const productRye = { id: 2, sku: "BRD-002", title: "Rye Bread", description: null, price: 12.4, category: "bread", status: "draft", inventory: 6 };
const productJam = { id: 3, sku: "JAM-014", title: "Strawberry Jam", description: "Small batch strawberry preserve.", price: 4.1, category: "preserves", status: "active", inventory: 0 };
const productsBody = { count: 3, products: [productSourdough, productRye, productJam] };
// A search for "sourdough" matches only the first product (title ilike in list_products).
const searchedBody = { count: 1, products: [productSourdough] };
const analyticsBody = { catalog: { product_count: 3 }, inventory: { low_stock_count: 2 }, sales: { order_count_total: 8, total_revenue: 1250.75, orders_last_30_days: 3 } };

// Render the catalog view; record every call as [method, href].
// A scripted answer may be a plain body, a ready Response or a function of the request count.
const asResponse = (value) => (value instanceof Response ? value : json(value));
const resolveAnswer = (answer) => asResponse(typeof answer === "function" ? answer() : answer);

function renderCatalog({ product = null, list = productsBody, analytics = analyticsBody } = {}) {
  resetEnv();
  resetDom();
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (method === "GET" && href.startsWith("/analytics/summary")) return resolveAnswer(analytics);
    const one = /^\/products\/(\d+)$/.exec(href);
    if (method === "GET" && one) {
      if (product) return Promise.resolve(product(Number(one[1])));
      const found = productsBody.products.find((p) => p.id === Number(one[1]));
      return found ? json(found) : json({ detail: "Product not found" }, 404);
    }
    if (method === "GET" && /^\/products(\?.*)?$/.test(href)) {
      const q = new URLSearchParams(href.split("?")[1] ?? "");
      return resolveAnswer(q.get("search") ? searchedBody : list);
    }
    throw new Error(`unexpected fetch ${method} ${href}`);
  });
  const cleanup = catalogView(host); // the view hands back its unmount hook; expose it for the debounce test
  return { host, calls, cleanup };
}

// Poll one macrotask at a time until the view has settled; never a fixed sleep.
async function waitFor(fn, what) {
  for (let i = 0; i < 100; i++) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 0));
  }
  throw new Error(`timed out waiting for ${what}`);
}

// Flush promise chains once setTimeout is mocked: real setImmediate still turns.
async function flush() {
  for (let i = 0; i < 50; i++) await new Promise((r) => setImmediate(r));
}

const settled = (host) => host.querySelectorAll("article.metric").length === 4 && host.querySelector("table") !== null && !host.querySelector("[aria-busy]");
const listCalls = (calls) => calls.filter(([m, href]) => m === "GET" && /^\/products(\?|$)/.test(href));
const queryOf = (call) => new URLSearchParams(call[1].split("?")[1] ?? ""); // call = [method, href]

// Two products with the optional fields left out: no category and no inventory on the first.
const sparseProductsBody = { count: 2, products: [
  { id: 10, sku: "MYS-001", title: "Mystery Item", price: 9.5, status: "active" },
  { id: 11, sku: "BRD-003", title: "Wholegrain Loaf", description: null, price: 7.2, category: "bread", status: "draft", inventory: 4 },
] };

// Three rows with the gap in the middle, so a sort comparison meets a missing field on both
// sides (the ?? fallbacks for av and bv) instead of only at one end of the list.
const gappyProductsBody = { count: 3, products: [
  { id: 20, sku: "GRN-101", title: "Green Loaf", price: 5.5, category: "bread", status: "active", inventory: 9 },
  { id: 21, sku: "MYS-102", title: "Mystery Two", price: 8.25, status: "draft" }, // no category, no inventory
  { id: 22, sku: "GRN-103", title: "Grain Roll", price: 6.75, category: "bread", status: "active", inventory: 2 },
] };

const statsPanel = (host) => host.children[1]; // page-head, then the metrics grid, then the products card
const tablePanel = (host) => host.querySelector("div.card-body.flush");
const skus = (host) => [...host.querySelectorAll("tbody tr")].map((r) => r.querySelector("td.mono").textContent);

test("the catalog renders its header and loads both panels from the API", async () => {
  const { host, calls } = renderCatalog();
  await waitFor(() => settled(host), "the catalog to settle");
  assert.equal(host.querySelector("h1").textContent, "Catalog");
  assert.equal(host.children[0].querySelector("p").textContent, "Products, stock levels and sales performance across the workspace.");
  assert.equal(host.querySelectorAll(".error-state").length, 0);
  assert.deepEqual(calls.map(([m, href]) => `${m} ${href.split("?")[0]}`).sort(), ["GET /analytics/summary", "GET /products"]);
});

test("the stat cards show products, revenue with AOV, orders and low stock from /analytics/summary", async () => {
  const { host } = renderCatalog();
  await waitFor(() => settled(host), "the catalog to settle");

  const cards = [...host.querySelectorAll("article.metric")];
  assert.deepEqual(cards.map((c) => c.querySelector(".metric-label").textContent), ["Products", "Revenue", "Orders", "Low stock"]);
  assert.deepEqual(cards.map((c) => c.querySelector(".metric-value").textContent), ["3", "$1,251", "8", "2"]);
  assert.equal(cards[0].querySelector(".metric-sub").textContent, "tracked in this workspace");
  assert.equal(cards[1].querySelector(".metric-sub").textContent, "$156 average order"); // 1250.75 / 8 orders
  assert.equal(cards[2].querySelector(".metric-sub").textContent, "3 in the last 30 days");
  assert.equal(cards[3].querySelector(".metric-sub").textContent, "at or below reorder point");
  assert.equal(cards[3].querySelector(".metric-value").style.color, "var(--warning)"); // low stock tints the value
});

test("the product table lists sku, title, category, status, price and stock in API order", async () => {
  const { host } = renderCatalog();
  await waitFor(() => settled(host), "the catalog to settle");

  const rows = [...host.querySelector("tbody").querySelectorAll("tr")];
  assert.deepEqual(rows.map((r) => r.querySelector("td.mono").textContent), ["BRD-001", "BRD-002", "JAM-014"]);
  assert.deepEqual(rows.map((r) => r.querySelector(".truncate").getAttribute("title")), ["Sourdough Loaf", "Rye Bread", "Strawberry Jam"]);

  const jam = rows[2]; // zero stock: danger tint, preserves category badge
  assert.equal(jam.children[2].querySelector("span.badge").textContent, "Preserves");
  assert.equal(jam.children[3].querySelector("span.badge").getAttribute("data-tone"), "success");
  assert.equal(jam.children[4].textContent, "$4"); // money(4.1) rounds to whole dollars
  assert.equal(jam.children[5].textContent, "0");
  assert.equal(jam.children[5].style.color, "var(--danger)");

  const rye = rows[1]; // six left: warning tint; draft stays a neutral badge
  assert.equal(rye.children[3].querySelector("span.badge").getAttribute("data-tone"), "neutral");
  assert.equal(rye.children[5].style.color, "var(--warning)");

  const foot = host.querySelector(".card-foot");
  assert.equal(foot.textContent, "3 productsbread 2preserves 1"); // count plus the category breakdown dots
});

test("typing in the search box debounces and requests the trimmed term with limit 100", async (t) => {
  const { host, calls } = renderCatalog();
  await waitFor(() => settled(host), "the catalog to settle");
  const firstQuery = queryOf(listCalls(calls).at(-1));
  assert.equal(firstQuery.get("limit"), "100"); // the initial list asks for up to 100 rows
  assert.equal(firstQuery.has("search"), false);

  t.mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const search = host.querySelector("input.input");
    search.value = "  Sourdough  ";
    search.dispatchEvent(new Event("input"));
    search.value = "Sourdough"; // a fast second keystroke inside the debounce window
    search.dispatchEvent(new Event("input"));
    t.mock.timers.tick(280); // one debounced reload, not one per keystroke
    await flush();

    const lists = listCalls(calls);
    assert.equal(lists.length, 2);
    const q = queryOf(lists.at(-1));
    assert.equal(q.get("search"), "Sourdough");
    assert.equal(q.get("limit"), "100");
    const rows = [...host.querySelector("tbody").querySelectorAll("tr")];
    assert.deepEqual(rows.map((r) => r.querySelector("td.mono").textContent), ["BRD-001"]);
    assert.equal(host.querySelector(".card-foot").textContent, "1 product"); // singular, no breakdown for one category
  } finally {
    t.mock.timers.reset();
  }
});

test("an empty catalog shows the no-products plate instead of a table", async () => {
  const { host } = renderCatalog({ list: { count: 0, products: [] } });
  await waitFor(() => host.querySelector(".empty") !== null, "the empty plate");

  assert.equal(host.querySelector("table"), null);
  const plate = host.querySelector(".empty");
  assert.equal(plate.querySelector("h3").textContent, "No products found");
  assert.equal(plate.querySelector("p").textContent, "Nothing matches this search, or the catalog has not been populated yet.");
});

test("clicking a row opens the product dialog from /products/{id} and closes on Close", async () => {
  const { host } = renderCatalog();
  await waitFor(() => settled(host), "the catalog to settle");

  const row = host.querySelector("tbody").querySelectorAll("tr")[0]; // Sourdough, id 1
  row.click();
  const dlg = document.querySelector("dialog");
  assert.ok(dlg && dlg.open, "the dialog opens immediately with a skeleton body");
  await waitFor(() => dlg.querySelector(".dialog-body").querySelector("h3") !== null, "the product to load into the dialog");

  assert.equal(dlg.querySelector("#dlg-title").textContent, "Product");
  const body = dlg.querySelector(".dialog-body");
  assert.equal(body.querySelector("h3").textContent, "Sourdough Loaf");
  assert.deepEqual([...body.querySelectorAll(".chip")].map((c) => c.textContent), ["BRD-001"]);
  const badges = [...body.querySelectorAll("span.badge")];
  assert.deepEqual(badges.map((b) => b.textContent), ["Active", "Bread"]); // status badge then category badge
  assert.equal(badges[0].getAttribute("data-tone"), "success");
  assert.equal(body.querySelector("p").textContent, "Naturally leavened wheat bread.");

  const kv = body.querySelector(".kv");
  assert.deepEqual([...kv.querySelectorAll("dt")].map((d) => d.textContent), ["Price", "Inventory", "Category", "Product ID"]);
  assert.deepEqual([...kv.querySelectorAll("dd")].map((d) => d.textContent), ["$7", "42", "Bread", "#1"]);

  const close = dlg.querySelector(".dialog-foot").querySelector("button");
  assert.equal(close.textContent, "Close");
  assert.equal(close.getAttribute("data-variant"), "ghost");
  close.click();
  assert.equal(document.querySelector("dialog"), null); // the close event removes it from the body
});

test("a missing product shows the API's 404 message inside the dialog", async () => {
  const notFound = (id) => json({ detail: "Product not found" }, 404);
  const { host } = renderCatalog({ product: notFound });
  await waitFor(() => settled(host), "the catalog to settle");

  const row = host.querySelector("tbody").querySelectorAll("tr")[1]; // Rye, id 2
  row.click();
  const dlg = document.querySelector("dialog");
  assert.ok(dlg, "the dialog opens even when the fetch fails");
  await waitFor(() => dlg.querySelector(".error-state") !== null, "the dialog to show its error");

  const err = dlg.querySelector(".error-state");
  assert.equal(err.getAttribute("role"), "alert");
  assert.equal(err.querySelector("p").textContent, "Product not found"); // api.js lifts detail into the message
});

test("an analytics API error shows an error state and retry recovers the metrics", async () => {
  let attempts = 0;
  const failing = () => (++attempts === 1 ? json({ detail: "The warehouse is down." }, 503) : analyticsBody);
  const { host } = renderCatalog({ analytics: failing });
  await waitFor(() => statsPanel(host).querySelector(".error-state") !== null, "the stats error");

  const err = statsPanel(host).querySelector(".error-state");
  assert.equal(err.getAttribute("role"), "alert");
  assert.equal(err.querySelector("h3").textContent, t("ui.error"));
  assert.equal(err.querySelector("p").textContent, "The warehouse is down."); // api.js lifts detail into the message

  err.querySelector(".btn").click(); // retry re-runs the analytics load
  await waitFor(() => host.querySelectorAll("article.metric").length === 4, "the metrics after retry");
  assert.equal(attempts, 2);
});

test("a failed product list shows an error state and retry recovers the table", async () => {
  let attempts = 0;
  const failing = () => (++attempts === 1 ? json({ detail: "The shelf fell over." }, 500) : productsBody);
  const { host } = renderCatalog({ list: failing });
  await waitFor(() => tablePanel(host).querySelector(".error-state") !== null, "the products error");

  const err = tablePanel(host).querySelector(".error-state");
  assert.equal(err.querySelector("p").textContent, "The shelf fell over.");
  err.querySelector(".btn").click(); // retry re-runs the list load
  await waitFor(() => host.querySelector("tbody") !== null, "the table after retry");
  assert.deepEqual(skus(host), ["BRD-001", "BRD-002", "JAM-014"]);
});

test("analytics without optional sections renders zeros and no warning tone", async () => {
  const { host } = renderCatalog({ analytics: {} });
  await waitFor(() => settled(host), "the catalog to settle");

  const cards = [...host.querySelectorAll("article.metric")];
  assert.deepEqual(cards.map((c) => c.querySelector(".metric-value").textContent), ["0", "$0", "0", "0"]);
  assert.equal(cards[1].querySelector(".metric-sub").textContent, "$0 average order"); // no orders: AOV falls back to zero
  assert.equal(cards[3].querySelector(".metric-value").style.color, undefined); // no low stock: no warning tint
  assert.equal(cards[3].querySelector(".metric-sub").textContent, "every SKU is above its reorder point");
});

test("products without a category or stock render a dash and zero in danger", async () => {
  const { host } = renderCatalog({ list: sparseProductsBody });
  await waitFor(() => settled(host), "the catalog to settle");

  const rows = [...host.querySelector("tbody").querySelectorAll("tr")];
  assert.equal(rows[0].children[2].textContent, "—"); // no category badge, just the dash
  assert.equal(rows[0].children[5].textContent, "0"); // missing inventory counts as zero
  assert.equal(rows[0].children[5].style.color, "var(--danger)");

  const foot = host.querySelector(".card-foot");
  assert.equal(foot.textContent, "2 productsuncategorised 1bread 1"); // the dash bucket joins the breakdown
});

test("the product dialog falls back for a missing description and category", async () => {
  const mystery = (id) => json({ id: 10, sku: "MYS-001", title: "Mystery Item", price: 9.5, status: "active", inventory: 3 });
  const { host } = renderCatalog({ list: sparseProductsBody, product: mystery });
  await waitFor(() => settled(host), "the catalog to settle");

  host.querySelector("tbody tr").click(); // the first row is the category-less item
  const dlg = document.querySelector("dialog");
  await waitFor(() => dlg.querySelector(".dialog-body h3") !== null, "the product in the dialog");

  const body = dlg.querySelector(".dialog-body");
  assert.deepEqual([...body.querySelectorAll(".chip")].map((c) => c.textContent), ["MYS-001"]); // no category chip
  const badges = [...body.querySelectorAll("span.badge")];
  assert.equal(badges.length, 1); // the status badge only
  assert.equal(body.querySelector("p").textContent, "No description yet. A good candidate for the agent to rewrite.");

  const kv = body.querySelector(".kv");
  assert.deepEqual([...kv.querySelectorAll("dd")].map((d) => d.textContent), ["$10", "3", "Uncategorised", "#10"]);
});

test("clicking a sortable header sorts ascending, then descending on the second click", async () => {
  const { host } = renderCatalog();
  await waitFor(() => settled(host), "the catalog to settle");

  const ths = [...host.querySelectorAll("thead th")];
  const titleTh = ths[1]; // Title
  assert.equal(titleTh.getAttribute("aria-sort"), "none"); // the default order is by id, no header owns it

  titleTh.click();
  assert.deepEqual(skus(host), ["BRD-002", "BRD-001", "JAM-014"]); // Rye < Sourdough < Strawberry
  assert.equal(titleTh.getAttribute("aria-sort"), "ascending");
  assert.ok(ths.filter((th) => th !== titleTh).every((th) => th.getAttribute("aria-sort") === "none"));

  titleTh.click();
  assert.deepEqual(skus(host), ["JAM-014", "BRD-001", "BRD-002"]); // the second click flips the direction
  assert.equal(titleTh.getAttribute("aria-sort"), "descending");
});

test("numeric and sparse headers sort by value, missing fields as zero or empty", async () => {
  const { host } = renderCatalog({ list: sparseProductsBody });
  await waitFor(() => settled(host), "the catalog to settle");

  const ths = [...host.querySelectorAll("thead th")];
  ths[5].click(); // Stock, numeric: the missing inventory sorts as zero
  assert.deepEqual(skus(host), ["MYS-001", "BRD-003"]);
  assert.equal(ths[5].getAttribute("aria-sort"), "ascending");

  ths[2].click(); // Category, string: the missing category sorts before "bread"
  assert.deepEqual(skus(host), ["MYS-001", "BRD-003"]);
});

test("keyboard: Enter and space toggle a header sort, Enter opens a row", async () => {
  const { host } = renderCatalog();
  await waitFor(() => settled(host), "the catalog to settle");

  const keydown = (el, key) => { const e = new Event("keydown"); e.key = key; el.dispatchEvent(e); };
  const titleTh = [...host.querySelectorAll("thead th")][1]; // Title

  keydown(titleTh, "a"); // an unrelated key does nothing
  assert.deepEqual(skus(host), ["BRD-001", "BRD-002", "JAM-014"]); // still the id order

  keydown(titleTh, " "); // space toggles like a click and prevents the default scroll
  assert.equal(titleTh.getAttribute("aria-sort"), "ascending");
  assert.deepEqual(skus(host), ["BRD-002", "BRD-001", "JAM-014"]);

  keydown(titleTh, "Enter"); // Enter toggles a header too, flipping back to descending
  assert.equal(titleTh.getAttribute("aria-sort"), "descending");
  assert.deepEqual(skus(host), ["JAM-014", "BRD-001", "BRD-002"]);

  const row = host.querySelector("tbody tr"); // now Strawberry Jam, id 3
  keydown(row, "Tab"); // not Enter: no dialog opens
  assert.equal(document.querySelector("dialog"), null);
  keydown(row, "Enter");
  assert.ok(document.querySelector("dialog")?.open);
});

test("a list answer without a products key also shows the empty plate", async () => {
  const { host } = renderCatalog({ list: {} }); // no .products anywhere in the answer
  await waitFor(() => host.querySelector(".empty") !== null, "the empty plate");
  assert.equal(host.querySelector("table"), null);
});

test("sorting a gappy column meets missing fields on both sides of a comparison", async () => {
  const { host } = renderCatalog({ list: gappyProductsBody });
  await waitFor(() => settled(host), "the catalog to settle");

  const ths = [...host.querySelectorAll("thead th")];
  ths[5].click(); // Stock, numeric: the missing inventory sorts as zero between two stocked rows
  assert.deepEqual(skus(host), ["MYS-102", "GRN-103", "GRN-101"]);

  ths[2].click(); // Category, string: the missing category sorts before "bread" on both sides
  assert.deepEqual(skus(host), ["MYS-102", "GRN-101", "GRN-103"]);
});

test("unmounting clears a pending debounced search", async (t) => {
  const { host, calls, cleanup } = renderCatalog();
  await waitFor(() => settled(host), "the catalog to settle");
  t.mock.timers.enable({ apis: ["setTimeout"] }); // clearTimeout is mocked alongside it
  try {
    const search = host.querySelector("input.input");
    search.value = "Jam";
    search.dispatchEvent(new Event("input")); // schedules a reload 280ms out
    cleanup(); // the route changes before it fires
    t.mock.timers.tick(280);
    await flush();
    assert.equal(listCalls(calls).length, 1); // only the initial list: the pending reload was cancelled
  } finally {
    t.mock.timers.reset();
  }
});

test("clearing the search to blanks reloads without a search param", async (t) => {
  const { host, calls } = renderCatalog();
  await waitFor(() => settled(host), "the catalog to settle");
  t.mock.timers.enable({ apis: ["setTimeout"] });
  try {
    const search = host.querySelector("input.input");
    search.value = "   "; // only spaces: trims to an empty term
    search.dispatchEvent(new Event("input"));
    t.mock.timers.tick(280);
    await flush();

    const q = queryOf(listCalls(calls).at(-1));
    assert.equal(q.has("search"), false); // term || undefined drops the param
    assert.deepEqual(skus(host), ["BRD-001", "BRD-002", "JAM-014"]); // back to the full list
  } finally {
    t.mock.timers.reset();
  }
});
