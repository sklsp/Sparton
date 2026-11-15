// start.js on the DOM fake: onboarding in three steps. The shop URL is normalised before the call, a plan
// limit (402) is shown in the customer's words, suggested rivals start unticked and manual ones ticked,
// "go" stays disabled until one is chosen, a retry does not create a rival twice, and the first check
// reports real progress from the competitors themselves. Fixtures mirror app/api/commerce.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake.
import startView from "../../app/web/views/start.js";
import { t } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { Event, resetDom } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n = 6) => { for (let i = 0; i < n; i++) await tick(); };

const shop = { id: 5, name: "devries.nl", url: "https://devries.nl", domain: "devries.nl" };
const rival = (id, domain, extra = {}) => ({ id, shop_id: 5, domain, name: domain, url: `https://${domain}`, last_crawled_at: null, last_status: null, data_source: null, product_count: 0, ...extra });

function routes(state) {
  return (href, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : null;
    if (href === "/shops" && init.method === "POST") {
      state.shopBodies.push(body);
      if (state.shopError) return json({ detail: state.shopError.detail }, state.shopError.status);
      return json(shop, 201);
    }
    if (href === "/shops/5/discover" && init.method === "POST") return json({ suggestions: state.suggestions });
    if (href === "/competitors" && init.method === "POST") {
      state.created.push(body.url);
      if (state.failOn === body.url) { state.failOn = null; return json({ detail: "That is not a shop" }, 400); }
      return json(rival(state.created.length, new URL(body.url).hostname), 201);
    }
    if (href === "/shops/5/crawl" && init.method === "POST") { state.crawls += 1; return json({ queued: 1 }, 202); }
    if (href === "/competitors?shop_id=5") return json({ count: state.competitors.length, competitors: state.competitors });
    throw new Error(`unexpected fetch ${href}`);
  };
}

const base = (over = {}) => ({ shopBodies: [], created: [], crawls: 0, suggestions: [], competitors: [], ...over });

async function render(state) {
  resetEnv(); resetDom(); resetToasts();
  setFetch(routes(state));
  const host = document.createElement("main");
  const stop = startView(host, { navigate: () => {} });
  await settle(1);
  return { host, stop };
}

const submit = (form) => form.dispatchEvent(new Event("submit"));
const stopNamed = (host, id) => host.querySelector(".ob-rail").children.find((li) => li.dataset.step === id);

async function enterShop(host, value) {
  host.querySelector("#ob-url").value = value;
  submit(host.querySelector("form.ob-plate"));
  await settle();
}

test("step one asks for the shop and marks the first stop current", async () => {
  const { host, stop } = await render(base());
  assert.equal(host.querySelector("h1.view-title").textContent, t("ob.title"));
  assert.equal(stopNamed(host, "shop").getAttribute("aria-current"), "step");
  assert.equal(stopNamed(host, "rivals").getAttribute("aria-current"), null);
  assert.equal(document.activeElement, host.querySelector("#ob-url")); // focus lands in the URL field
  stop();
});

test("a bare domain is sent as an https URL and leads to step two", async () => {
  const state = base();
  const { host, stop } = await render(state);
  await enterShop(host, "  devries.nl ");
  assert.deepEqual(state.shopBodies, [{ url: "https://devries.nl" }]);
  assert.equal(stopNamed(host, "shop").hasAttribute("data-done"), true);
  assert.equal(stopNamed(host, "rivals").getAttribute("aria-current"), "step");
  stop();
});

test("a plan limit is shown in the customer's words, other errors as sent", async () => {
  const limited = base({ shopError: { status: 402, detail: { error: "plan_limit_reached", message: "Your Free plan allows 1 shop.", upgrade: true } } });
  const one = await render(limited);
  await enterShop(one.host, "devries.nl");
  assert.equal(one.host.querySelector("p.form-error").textContent, "Your Free plan allows 1 shop.");
  assert.equal(stopNamed(one.host, "shop").getAttribute("aria-current"), "step"); // still on step one
  one.stop();
  const taken = base({ shopError: { status: 409, detail: "You have already added this shop" } });
  const two = await render(taken);
  await enterShop(two.host, "devries.nl");
  assert.equal(two.host.querySelector("p.form-error").textContent, "You have already added this shop");
  two.stop();
});

test("suggested rivals start unticked; go stays disabled until one is chosen", async () => {
  const state = base({ suggestions: [{ url: "https://jansen.nl", domain: "jansen.nl" }, { url: "https://broodhuis.nl", domain: "broodhuis.nl" }] });
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  const list = host.querySelector("ul.ob-suggestions");
  assert.equal(list.hasAttribute("aria-busy"), false);
  const rows = list.querySelectorAll("li.ob-suggestion");
  assert.equal(rows.length, 2);
  assert.ok(rows[0].textContent.includes("jansen.nl"));
  const go = host.querySelectorAll("button").find((b) => b.textContent.includes(t("ob.rivals.go")));
  assert.equal(host.querySelector(".ob-count").textContent, t("ob.rivals.count", { n: 0 }));
  const box = rows[1].querySelector("input");
  box.checked = true;
  box.dispatchEvent(new Event("change"));
  assert.equal(host.querySelector(".ob-count").textContent, t("ob.rivals.count", { n: 1 }));
  assert.equal(go.disabled, false);
  stop();
});

test("no suggestions says so, and a manual rival replaces that line, ticked", async () => {
  const state = base();
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  assert.ok(host.querySelector("li.ob-none").textContent.includes(t("ob.rivals.none")));
  host.querySelector("#ob-rival").value = "www.jansen.nl";
  submit(host.querySelector("form.ob-manual"));
  assert.equal(host.querySelector("li.ob-none"), null);
  const row = host.querySelector("li.ob-suggestion");
  assert.ok(row.textContent.includes("jansen.nl"));
  assert.ok(row.textContent.includes(t("ob.rivals.yours")));
  assert.equal(host.querySelector(".ob-count").textContent, t("ob.rivals.count", { n: 1 }));
  assert.equal(host.querySelector("#ob-rival").value, ""); // cleared for the next one
  stop();
});

test("a refused rival can be retried without creating the others twice", async () => {
  const state = base({ failOn: "https://broodhuis.nl" });
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  for (const v of ["jansen.nl", "broodhuis.nl"]) {
    host.querySelector("#ob-rival").value = v;
    submit(host.querySelector("form.ob-manual"));
  }
  const go = host.querySelectorAll("button").find((b) => b.textContent.includes(t("ob.rivals.go")));
  go.click();
  await settle();
  assert.equal(host.querySelector("p.form-error").textContent, "That is not a shop");
  assert.equal(state.crawls, 0);
  go.click(); // the second attempt goes through
  await settle();
  assert.deepEqual(state.created, ["https://jansen.nl", "https://broodhuis.nl", "https://broodhuis.nl"]);
  assert.equal(state.crawls, 1);
  assert.equal(stopNamed(host, "check").getAttribute("aria-current"), "step");
  stop();
});

test("the first check reports progress from the competitors themselves", async () => {
  const state = base({ competitors: [
    rival(1, "jansen.nl", { last_status: "COMPLETED", last_crawled_at: "2026-11-15T06:00:00Z", data_source: "feed", product_count: 48 }),
    rival(2, "broodhuis.nl", { last_status: "RUNNING" }),
  ] });
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  host.querySelector("#ob-rival").value = "jansen.nl";
  submit(host.querySelector("form.ob-manual"));
  host.querySelectorAll("button").find((b) => b.textContent.includes(t("ob.rivals.go"))).click();
  await settle(10);
  const rows = host.querySelectorAll("li.ob-row");
  assert.deepEqual(rows.map((r) => r.dataset.state), ["exact", "reading"]);
  assert.equal(host.querySelector('[role="status"]').textContent, t("ob.check.progress", { done: 1, n: 2 }));
  assert.equal(host.querySelector("a.btn").getAttribute("href"), "#/overview");
  stop(); // clears the next poll
});

test("when every competitor is read the check says the baseline is set", async () => {
  const state = base({ competitors: [
    rival(1, "jansen.nl", { last_status: "COMPLETED", last_crawled_at: "2026-11-15T06:00:00Z", data_source: "html", product_count: 12 }),
    rival(2, "broodhuis.nl", { last_status: "BLOCKED", last_crawled_at: "2026-11-15T06:01:00Z" }),
  ] });
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  host.querySelector("#ob-rival").value = "jansen.nl";
  submit(host.querySelector("form.ob-manual"));
  host.querySelectorAll("button").find((b) => b.textContent.includes(t("ob.rivals.go"))).click();
  await settle(10);
  assert.deepEqual(host.querySelectorAll("li.ob-row").map((r) => r.dataset.state), ["extracted", "failed"]);
  assert.equal(host.querySelector('[role="status"]').textContent, t("ob.check.allDone"));
  stop();
});
