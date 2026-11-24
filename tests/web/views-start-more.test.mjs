// start.js on the DOM fake, second pass: the branch sides views-start.test.mjs leaves unexercised. A failed
// discovery drops the spinner and says no rivals were found (lines 122-124), a zero-suggestion answer does
// the same through the success path; a plan limit without a message falls back to the generic wording;
// unchecking a rival updates the count and disables go again; an empty manual entry is ignored; a poll that
// answers with a bare array still renders, and a failed poll keeps the queued summary and retries; waiting,
// failed and jsonld competitors are worded correctly; a URL that already has a scheme is kept, and an
// unparseable one keeps its raw label. Fixtures mirror app/api/commerce.py.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake.
import startView from "../../app/web/views/start.js";
import { t } from "../../app/web/i18n.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { Event, resetDom } from "./dom.mjs"; // dom's own Event: Node's built-in has a getter-only target

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const settle = async (n = 6) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0)); };

const rival = (id, domain, over = {}) => ({ id, shop_id: 5, domain, name: null, data_source: "html", last_status: "COMPLETED", last_crawled_at: "2026-11-08T07:40:00Z", product_count: 3, ...over });
const sugg = (domain) => ({ url: `https://${domain}`, domain });

function routes(state) {
  return (href, init = {}) => {
    if (init.method === "POST" && href === "/shops") {
      if (state.shopStatus) return json({ detail: state.shopDetail }, state.shopStatus);
      state.shopBodies.push(JSON.parse(init.body));
      return json({ id: 5 }, 201);
    }
    if (init.method === "POST" && href === "/shops/5/crawl") { state.crawls += 1; return json({ status: "QUEUED" }, 202); }
    if (init.method === "POST" && href === "/shops/5/discover") return state.discoverStatus ? json({ detail: state.discoverDetail }, state.discoverStatus) : json(state.discovered ?? { suggestions: [] });
    if (init.method === "POST" && href === "/competitors") { state.competitorBodies.push(JSON.parse(init.body)); return json({ id: 90, shop_id: 5, domain: JSON.parse(init.body).domain }, 201); }
    if (href === "/competitors?shop_id=5") {
      if (state.pollFail) { state.pollFail = false; return json({ detail: "database down" }, 500); }
      return state.bareArray ? json(state.competitors) : json({ count: state.competitors.length, competitors: state.competitors });
    }
    throw new Error(`unexpected fetch ${href}`);
  };
}

function base(over = {}) {
  return { shopBodies: [], competitorBodies: [], crawls: 0, discovered: { suggestions: [sugg("bakkerijjansen.nl"), sugg("broodhuis.nl")] }, competitors: [], ...over };
}

async function render(state) {
  resetEnv(); resetDom(); resetToasts();
  setFetch(routes(state));
  const host = document.createElement("main");
  const stop = startView(host, { navigate: () => {} });
  await settle();
  return { host, stop };
}

const submit = (form) => form.dispatchEvent(new Event("submit"));

async function enterShop(host, url, state) {
  host.querySelector("#ob-url").value = url;
  await submit(host.querySelector("form.ob-plate"));
  await settle();
  return state;
}

test("a failed discovery drops the spinner and says no rivals were found", async () => {
  const state = base({ discoverStatus: 500, discoverDetail: "database down" });
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  const list = host.querySelector("ul.ob-suggestions");
  assert.equal(list.hasAttribute("aria-busy"), false);
  assert.ok(list.querySelector(".ob-searching") === null);
  assert.ok(list.querySelector("li.ob-none").textContent.includes(t("ob.rivals.none")));
  stop();
});

test("a plan limit without a message falls back to the generic wording", async () => {
  const state = base({ shopStatus: 402, shopDetail: "limit reached" }); // string detail has no .message
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  assert.equal(host.querySelector("p.form-error").textContent, t("ob.err.limit"));
  stop();
});

test("unchecking a rival updates the count and disables go again", async () => {
  const state = base();
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  const box = host.querySelectorAll("li.ob-suggestion")[0].querySelector("input");
  const go = host.querySelectorAll("button").find((b) => b.textContent.includes(t("ob.rivals.go")));
  box.checked = true;
  box.dispatchEvent(new Event("change"));
  assert.equal(host.querySelector(".ob-count").textContent, t("ob.rivals.count", { n: 1 }));
  assert.equal(go.disabled, false);
  box.checked = false; // the else side of the checkbox handler
  box.dispatchEvent(new Event("change"));
  assert.equal(host.querySelector(".ob-count").textContent, t("ob.rivals.count", { n: 0 }));
  assert.equal(go.disabled, true);
  stop();
});

test("an empty manual entry is ignored and a missing suggestions key means none found", async () => {
  const state = base({ discovered: {} }); // no suggestions key -> the destructuring default applies
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  assert.ok(host.querySelector("li.ob-none").textContent.includes(t("ob.rivals.none")));
  host.querySelector("#ob-rival").value = "   "; // whitespace only
  await submit(host.querySelector("form.ob-manual"));
  assert.equal(host.querySelectorAll("li.ob-suggestion").length, 0); // nothing added
  assert.ok(host.querySelector("li.ob-none")); // the none line is still there
  stop();
});

test("a poll that answers with a bare array still renders", async () => {
  const state = base({ competitors: [rival(1, "jansen.nl")], bareArray: true });
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  host.querySelector("#ob-rival").value = "jansen.nl";
  await submit(host.querySelector("form.ob-manual"));
  host.querySelectorAll("button").find((b) => b.textContent.includes(t("ob.rivals.go"))).click();
  await settle(8);
  assert.equal(host.querySelectorAll("li.ob-row")[0].dataset.state, "extracted");
  assert.equal(host.querySelector('[role="status"]').textContent, t("ob.check.allDone")); // all final -> polling stops
  stop();
});

test("a failed poll keeps the queued summary and retries", async () => {
  const state = base({ competitors: [], pollFail: true });
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  host.querySelector("#ob-rival").value = "jansen.nl";
  await submit(host.querySelector("form.ob-manual"));
  host.querySelectorAll("button").find((b) => b.textContent.includes(t("ob.rivals.go"))).click();
  await settle(8); // the first poll fails; a retry is scheduled, not an error shown
  assert.equal(host.querySelectorAll("li.ob-row").length, 0);
  assert.equal(host.querySelector('[role="status"]').textContent, t("ob.check.queued"));
  stop(); // clears the pending retry timer
});

test("waiting, failed and jsonld competitors are worded correctly", async () => {
  const state = base({ competitors: [
    rival(1, "waiting.nl", { last_status: null, last_crawled_at: null }),
    rival(2, "blocked.nl", { last_status: "FAILED" }),
    rival(3, "feedshop.nl", { data_source: "jsonld" }), // the jsonld side of the exact check
  ] });
  const { host, stop } = await render(state);
  await enterShop(host, "devries.nl");
  host.querySelector("#ob-rival").value = "jansen.nl";
  await submit(host.querySelector("form.ob-manual"));
  host.querySelectorAll("button").find((b) => b.textContent.includes(t("ob.rivals.go"))).click();
  await settle(8);
  assert.deepEqual([...host.querySelectorAll("li.ob-row")].map((r) => r.dataset.state), ["waiting", "failed", "exact"]);
  assert.equal(host.querySelector('[role="status"]').textContent, t("ob.check.progress", { done: 2, n: 3 })); // only the two finals count
  const text = host.textContent;
  assert.ok(text.includes(t("ob.state.waitingDetail")));
  assert.ok(text.includes(t("ob.state.failedDetail")));
  assert.ok(text.includes(t("ob.state.exactDetail", { n: 3 })));
  stop(); // clears the pending retry timer (not all rivals are final yet)
});

test("a URL that already has a scheme is kept and an unparseable one keeps its raw label", async () => {
  const state = base();
  const { host, stop } = await render(state);
  await enterShop(host, "https://devries.nl");
  assert.deepEqual(state.shopBodies, [{ url: "https://devries.nl" }]); // normalise did not double the scheme
  host.querySelector("#ob-rival").value = "https://[bad"; // a URL that throws when parsed
  await submit(host.querySelector("form.ob-manual"));
  const rows = host.querySelectorAll("li.ob-suggestion");
  assert.equal(rows.length, 3); // two suggestions plus the manual one
  assert.ok(rows[2].textContent.includes("https://[bad")); // hostOf fell back to the raw value
  assert.equal(host.querySelector(".ob-count").textContent, t("ob.rivals.count", { n: 1 }));
  stop();
});
