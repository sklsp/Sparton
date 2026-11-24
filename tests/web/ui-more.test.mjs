// The ui.js helpers the view tests only touch indirectly: the remaining h() prop paths,
// bytes/num/money edge values, every ago()/when() bucket (including the >30-day branch at
// ui.js:142 that produces the same output as the unreachable final return pinned in
// ui-dom.test.mjs), tone(), and the composite builders (card, metric, button, skeleton,
// empty, errorState, banner, meter) plus statusBadge and toast/dialog/confirmDialog/asyncPanel branches.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import { Event, resetDom, setMedia } from "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import * as ui from "../../app/web/ui.js";
import { t } from "../../app/web/i18n.js"; // for asserting i18n-sourced defaults (module-cached: ui.js already loaded it)

// One macrotask tick: enough for a resolved promise's .then/.catch chain to flush.
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test("h() falls back to div when the spec has no tag", () => {
  const el = ui.h(".ghost");
  assert.equal(el.tagName, "DIV");
  assert.ok(el.classList.contains("ghost"));
  const solo = ui.h("#solo");
  assert.equal(solo.id, "solo");
});

test("h() treats an element or array in the props slot as children", () => {
  const child = ui.h("span", "kid");
  const el = ui.h("div", child);
  assert.equal(el.children.length, 1);
  assert.equal(el.children[0], child);
  const flat = ui.h("div", ["a", "b"]);
  assert.deepEqual(flat.childNodes.map((n) => n.textContent), ["a", "b"]);
});

test("h() writes unknown booleans as empty attributes and known props as properties", () => {
  const el = ui.h("div", { hidden: true, lang: "en" });
  assert.equal(el.getAttribute("hidden"), "");
  assert.equal(el.lang, "en");
});

test("bytes() formats zero, sub-KB and clamped values", () => {
  assert.equal(ui.bytes(0), "0 B");
  assert.equal(ui.bytes(-5), "0 B");
  assert.equal(ui.bytes(null), "0 B");
  assert.equal(ui.bytes("nope"), "0 B");
  assert.equal(ui.bytes(512), "512 B");
  assert.equal(ui.bytes(2048), "2.0 KB");
  assert.equal(ui.bytes(5 * 1024 ** 3), "5.0 GB");
  assert.equal(ui.bytes(10 * 1024 ** 4), "10240.0 GB"); // clamps at the last unit
});

test("num() and money() treat missing values as zero", () => {
  assert.equal(ui.num(null), "0");
  assert.equal(ui.money(undefined), "$0");
});

test("ago() covers every reachable bucket", () => {
  const now = Date.now();
  assert.equal(ui.ago(null), "—");
  assert.equal(ui.ago(""), "—");
  assert.equal(ui.ago("not a date"), "—");
  assert.equal(ui.ago(new Date(now - 10 * 1000).toISOString()), "just now");
  // No tz suffix: ago() appends Z before parsing, as it does for server strings without one.
  assert.equal(ui.ago(new Date(now - 5 * 60 * 1000).toISOString().replace(/Z$/, "")), "5m ago");
  assert.equal(ui.ago(new Date(now - 3 * 60 * 60 * 1000).toISOString()), "3h ago");
  assert.equal(ui.ago(new Date(now - 10 * 864e5).toISOString()), "10d ago");
  const old = new Date(now - 60 * 864e5);
  // >30 days takes the toLocaleDateString branch at ui.js:142 (the final return is dead code).
  assert.equal(ui.ago(old.toISOString()), old.toLocaleDateString());
});

test("when() renders valid dates and dashes for missing or invalid ones", () => {
  assert.equal(ui.when(null), "—");
  assert.equal(ui.when("garbage"), "—");
  const d = new Date();
  assert.equal(ui.when(d.toISOString()), d.toLocaleString());
});

test("tone() maps each status bucket and falls back to neutral", () => {
  assert.equal(ui.tone(null), "neutral");
  assert.equal(ui.tone("completed"), "success");
  assert.equal(ui.tone("failed"), "danger");
  assert.equal(ui.tone("pending"), "warning");
  assert.equal(ui.tone("running"), "info");
  assert.equal(ui.tone("mystery"), "neutral");
});

test("title() replaces separators with spaces and capitalizes word starts", () => {
  assert.equal(ui.title(null), "");
  assert.equal(ui.title("hello_world-x"), "Hello World X");
});

test("card() builds from a string heading or an options object", () => {
  const simple = ui.card("Plain");
  assert.equal(simple.tagName, "SECTION");
  assert.equal(simple.querySelector("h2").textContent, "Plain");
  assert.ok(!simple.querySelector(".sub"));
  assert.ok(!simple.querySelector(".card-head-actions"));
  assert.ok(!simple.querySelector(".card-foot"));
  assert.ok(simple.querySelector(".card-body")); // the body wrapper is always present

  const full = ui.card({ title: "T", sub: "S", actions: [ui.h("button.btn", "A")], flush: true, foot: "F" }, "body-content");
  assert.equal(full.querySelector(".sub").textContent, "S");
  assert.ok(full.querySelector(".card-head-actions .btn"));
  assert.ok(full.querySelector(".card-body.flush")); // flush lands on the body class, not the card's
  assert.equal(full.querySelector(".card-foot").textContent, "F");

  const bare = ui.card({});
  assert.ok(!bare.querySelector("header")); // no title: no head at all
  assert.ok(bare.querySelector(".card-body"));
});

test("metric() renders label and value with optional icon, sub and tone", () => {
  const full = ui.metric({ label: "L", value: "V", sub: "S", iconName: "check", tone: "success" });
  assert.equal(full.querySelector(".metric-label").textContent, "L");
  assert.equal(full.querySelector(".metric-value").textContent, "V");
  assert.ok(full.querySelector(".metric-icon svg"));
  assert.equal(full.querySelector(".metric-sub").textContent, "S");
  assert.equal(full.querySelector(".metric-value").style.color, "var(--success)"); // tone colors the value via a CSS variable

  const bare = ui.metric({ label: "L", value: "V" });
  assert.ok(!bare.querySelector(".metric-icon"));
  assert.ok(!bare.querySelector(".metric-sub"));
});

test("button() applies defaults, icon sizes and rest props", () => {
  const plain = ui.button("Go");
  assert.equal(plain.className, "btn");
  assert.equal(plain.type, "button");

  // variant/size land in data attributes; the icon is a bare svg child sized by size.
  const sm = ui.button("S", { variant: "primary", size: "sm", iconName: "check" });
  assert.equal(sm.getAttribute("data-variant"), "primary");
  assert.equal(sm.getAttribute("data-size"), "sm");
  assert.equal(sm.querySelector("svg").getAttribute("width"), "14");

  const md = ui.button("M", { size: "md", iconName: "check", type: "submit", class: "extra" });
  assert.equal(md.querySelector("svg").getAttribute("width"), "16");
  assert.equal(md.type, "submit");
  assert.ok(md.classList.contains("extra")); // rest props merge into the element's classes
});

test("skeleton() and skeletonMetrics() use their defaults", () => {
  assert.equal(ui.skeleton().children.length, 4);
  assert.equal(ui.skeleton(2).children.length, 2);
  assert.equal(ui.skeletonMetrics().querySelectorAll("article.metric").length, 4); // default is four tiles
  assert.equal(ui.skeletonMetrics(1).querySelectorAll("article.metric").length, 1);
});

test("empty() renders the message and node or object actions", () => {
  const bare = ui.empty({ title: "None" });
  assert.ok(!bare.querySelector("p")); // no message: no paragraph at all
  assert.equal(bare.querySelector(".empty-icon svg").getAttribute("width"), "20");

  let clicked = false;
  const withAction = ui.empty({ title: "T", message: "M", action: { label: "Go", onClick: () => (clicked = true) } });
  assert.equal(withAction.querySelector("p").textContent, "M");
  withAction.querySelector(".btn").click(); // object actions become a primary button
  assert.ok(clicked);

  const nodeAction = ui.empty({ title: "T", action: ui.h("button.btn", "N") });
  assert.equal(nodeAction.querySelector(".btn").textContent, "N"); // ready nodes are appended as-is
});

test("errorState() shows the message and a working retry button", () => {
  const bare = ui.errorState({});
  assert.equal(bare.getAttribute("role"), "alert");
  assert.equal(bare.querySelector("h3").textContent, t("ui.error")); // default heading comes from i18n
  assert.ok(!bare.querySelector("p"));
  assert.ok(!bare.querySelector(".btn"));

  let retried = false;
  const full = ui.errorState({ title: "T", message: "M", onRetry: () => (retried = true) });
  assert.equal(full.querySelector("h3").textContent, "T");
  assert.equal(full.querySelector("p").textContent, "M");
  full.querySelector(".btn").click(); // the retry button carries onRetry
  assert.ok(retried);
});

test("banner() picks role and icon per tone", () => {
  const infoD = ui.icon("info").querySelector("path").getAttribute("d");
  const alertD = ui.icon("alert").querySelector("path").getAttribute("d");

  const info = ui.banner("hi");
  assert.equal(info.getAttribute("data-tone"), "info"); // default tone
  assert.equal(info.getAttribute("role"), "status");
  assert.equal(info.querySelector("svg path").getAttribute("d"), infoD);

  const warn = ui.banner("w", { tone: "warning" });
  assert.equal(warn.getAttribute("role"), "status");
  assert.equal(warn.querySelector("svg path").getAttribute("d"), alertD); // warning shares the alert glyph

  const danger = ui.banner("d", { tone: "danger" });
  assert.equal(danger.getAttribute("role"), "alert");

  const withAction = ui.banner("b", { action: ui.h("button.btn", "A") });
  assert.ok(withAction.querySelector(".banner-actions .btn"));
});

test("meter() clamps the fraction into [0, 1]", () => {
  const width = (v) => ui.meter(v).children[0].style.width; // the fill is a bare span child
  assert.equal(width(0.5), "50%");
  assert.equal(width(null), "0%");
  assert.equal(width(-3), "0%");
  assert.equal(width(1.5), "100%");
});

test("statusBadge() maps the status to a tone and adds the live dot when asked", () => {
  const idle = ui.statusBadge("completed");
  assert.equal(idle.getAttribute("data-tone"), "success");
  assert.equal(idle.children.length, 0); // no dot unless live

  const live = ui.statusBadge("running", true);
  assert.equal(live.getAttribute("data-tone"), "info");
  assert.equal(live.children[0].className, "dot dot-live");
});

test("scrollBehavior() respects prefers-reduced-motion", () => {
  resetDom(); // clears the media-query table
  assert.equal(ui.scrollBehavior(), "smooth");
  setMedia("(prefers-reduced-motion: reduce)", true);
  assert.equal(ui.scrollBehavior(), "auto");
});

test("toast() picks icons per tone, reuses the host and guards double close", () => {
  resetDom();
  ui.resetToasts();
  const c1 = ui.toast("Saved"); // returns a close function, not the element
  const info = document.querySelector(".toasts .toast");
  assert.equal(info.getAttribute("data-tone"), "info");
  const c2 = ui.toast("Boom", "danger");
  const danger = document.querySelectorAll(".toasts .toast")[1];
  assert.equal(danger.getAttribute("data-tone"), "danger");
  assert.equal(document.querySelectorAll(".toasts").length, 1); // host reused

  const infoD = ui.icon("info").querySelector("path").getAttribute("d");
  const alertD = ui.icon("alert").querySelector("path").getAttribute("d");
  assert.equal(info.querySelector("svg path").getAttribute("d"), infoD);
  assert.equal(danger.querySelector("svg path").getAttribute("d"), alertD);

  // The returned close() is idempotent: the second call returns before touching the element.
  const c3 = ui.toast("x");
  c3();
  c3();
  c1();
  c2();
  document.querySelectorAll(".toast").forEach((el) => el.dispatchEvent(new Event("animationend")));
  assert.equal(document.querySelector(".toasts")?.children.length ?? 0, 0);
});

test("dialog() works without an onClose callback", () => {
  resetDom();
  const el = ui.dialog({ title: "T", body: ui.h("p", "b") });
  assert.ok(el.open); // showModal sets the open flag
  assert.equal(document.querySelector("dialog"), el); // mounted on document.body

  el.close(); // no returnValue, no onClose: must not throw
  assert.equal(el.open, false);
  assert.equal(document.querySelector("dialog"), null); // removed via the close event
});

test("confirmDialog() resolves true on confirm, false on cancel or close", async () => {
  resetDom();
  const p1 = ui.confirmDialog({ title: "Sure?" });
  document.querySelector("dialog").querySelector(".dialog-foot").children[1].click(); // confirm button
  assert.equal(await p1, true);

  const p2 = ui.confirmDialog({ title: "Again?" });
  document.querySelector("dialog").querySelector(".dialog-foot").children[0].click(); // cancel button
  assert.equal(await p2, false);

  const p3 = ui.confirmDialog({ title: "Esc?" });
  document.querySelector("dialog").close(); // close event without a button click
  assert.equal(await p3, false);
});

test("asyncPanel() shows the custom loading node until the load settles", async () => {
  resetDom();
  const host = ui.h("div");
  let resolveLoad;
  const pending = new Promise((r) => (resolveLoad = r));
  ui.asyncPanel(host, () => pending, (data) => ui.h("span", "got:" + data), {
    loading: () => ui.h("div.loading-node", "loading…"),
  });
  assert.equal(host.querySelector(".loading-node")?.textContent, "loading…"); // custom loader while pending
  resolveLoad("x");
  await tick();
  assert.equal(host.querySelector("span")?.textContent, "got:x");
});

test("asyncPanel() ignores a stale reload when a newer one supersedes it", async () => {
  resetDom();
  const host = ui.h("div");
  let resolveA;
  const first = new Promise((r) => (resolveA = r));
  let resolveB;
  const second = new Promise((r) => (resolveB = r));
  const loads = [first, second];
  let call = 0;
  const rendered = [];
  const run = ui.asyncPanel(host, () => loads[call++], (data) => {
    rendered.push(data);
    return ui.h("span", "got:" + data);
  });
  // First load in flight (generation 1); start a second before it settles.
  run();
  resolveA("stale");
  await tick();
  assert.deepEqual(rendered, []); // superseded: never rendered
  resolveB("fresh");
  await tick();
  assert.deepEqual(rendered, ["fresh"]);
});

test("asyncPanel() swallows AbortError and shows an error state otherwise", async () => {
  resetDom();
  const host = ui.h("div");
  const abortErr = new Error("aborted");
  abortErr.name = "AbortError";
  ui.asyncPanel(host, () => Promise.reject(abortErr), (d) => ui.h("span", d));
  await tick();
  assert.ok(!host.querySelector(".error-state")); // aborted: no error UI

  const host2 = ui.h("div");
  let attempt = 0;
  ui.asyncPanel(
    host2,
    () => {
      attempt += 1;
      return attempt === 1 ? Promise.reject(new Error("boom")) : Promise.resolve("ok");
    },
    (d) => ui.h("span", "got:" + d),
  );
  await tick();
  assert.ok(host2.querySelector(".error-state"));
  host2.querySelector(".error-state .btn").click(); // retry re-runs the load
  await tick();
  assert.equal(attempt, 2);
  const spans = [...host2.querySelectorAll("span")];
  assert.equal(spans.at(-1).textContent, "got:ok");
});

