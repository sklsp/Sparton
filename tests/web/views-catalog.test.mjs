// catalog.js on the DOM fake: the four stat cards from /analytics/summary, the product table
// with its debounced server-side search, the empty plate and the per-product dialog from
// /products/{id}. Fixtures mirror the API shapes built in app/api/ecommerce.py (_product_dict,
// analytics_summary). BUG #1: api.js has no analytics(), products() or product() methods even
// though ecommerce.py serves those URLs, so every data test below stays todo until that is
// fixed; only the error-state behaviour is reachable today.
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
function renderCatalog({ product = null, list = productsBody } = {}) {
  resetEnv();
  resetDom();
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (method === "GET" && href.startsWith("/analytics/summary")) return json(analyticsBody);
    const one = /^\/products\/(\d+)$/.exec(href);
    if (method === "GET" && one) {
      if (product) return Promise.resolve(product(Number(one[1])));
      const found = productsBody.products.find((p) => p.id === Number(one[1]));
      return found ? json(found) : json({ detail: "Product not found" }, 404);
    }
    if (method === "GET" && /^\/products(\?.*)?$/.test(href)) {
      const q = new URLSearchParams(href.split("?")[1] ?? "");
      return json(q.get("search") ? searchedBody : list);
    }
    throw new Error(`unexpected fetch ${method} ${href}`);
  });
  catalogView(host);
  return { host, calls };
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
const queryOf = (href) => new URLSearchParams(href.split("?")[1] ?? "");

test("the catalog renders its header and settles into error states without a fetch", async () => {
  const { host, calls } = renderCatalog();
  await waitFor(() => host.querySelectorAll(".error-state").length === 2, "both panels to fail");

  assert.equal(host.querySelector("h1").textContent, "Catalog");
  assert.equal(host.children[0].querySelector("p").textContent, "Products, stock levels and sales performance across the workspace.");
  assert.ok(host.querySelector("input.input"), "the search box is present even when nothing loads");

  // BUG #1: api.js has no analytics() or products(), so both panels show the TypeError.
  const [statsError, productsError] = host.querySelectorAll(".error-state");
  for (const el of [statsError, productsError]) {
    assert.equal(el.getAttribute("role"), "alert");
    assert.equal(el.querySelector("h3").textContent, t("ui.error"));
    assert.ok([...el.querySelectorAll("button")].some((b) => b.textContent === t("ui.retry")), "a retry button is offered");
  }
  assert.equal(statsError.querySelector("p").textContent, "api.analytics is not a function");
  assert.equal(productsError.querySelector("p").textContent, "api.products is not a function");
  assert.deepEqual(calls, []); // the view never reaches the network
});

test("the stat cards show products, revenue with AOV, orders and low stock from /analytics/summary", { todo: "real bug, see sparton-quality-12/BUGS.md #1" }, async () => {
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

test("the product table lists sku, title, category, status, price and stock in API order", { todo: "real bug, see sparton-quality-12/BUGS.md #1" }, async () => {
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

test("typing in the search box debounces and requests the trimmed term with limit 100", { todo: "real bug, see sparton-quality-12/BUGS.md #1" }, async (t) => {
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
    t.mock.timers.restore();
  }
});

test("an empty catalog shows the no-products plate instead of a table", { todo: "real bug, see sparton-quality-12/BUGS.md #1" }, async () => {
  const { host } = renderCatalog({ list: { count: 0, products: [] } });
  await waitFor(() => host.querySelector(".empty") !== null, "the empty plate");

  assert.equal(host.querySelector("table"), null);
  const plate = host.querySelector(".empty");
  assert.equal(plate.querySelector("h3").textContent, "No products found");
  assert.equal(plate.querySelector("p").textContent, "Nothing matches this search, or the catalog has not been populated yet.");
});

test("clicking a row opens the product dialog from /products/{id} and closes on Close", { todo: "real bug, see sparton-quality-12/BUGS.md #1" }, async () => {
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

test("a missing product shows the API's 404 message inside the dialog", { todo: "real bug, see sparton-quality-12/BUGS.md #1" }, async () => {
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
