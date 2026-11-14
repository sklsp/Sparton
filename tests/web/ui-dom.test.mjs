// The ui.js builders against the small DOM fake from dom.mjs: h(), fill(), icon(),
// badge and statusBadge. Checked against what the dashboard shows (classes, tones, labels).
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake; both install on globalThis at import time.
import * as ui from "../../app/web/ui.js";

test("h() parses tag.class#id into element, classes and id", () => {
  const el = ui.h("section.card#main");
  assert.equal(el.tagName, "SECTION");
  assert.ok(el.classList.contains("card"));
  assert.equal(el.id, "main");
});

test("h() keeps several classes in order", () => {
  const el = ui.h("a.btn.primary");
  assert.equal(el.className, "btn primary");
});

test("h() appends the class prop to the spec's classes", () => {
  const el = ui.h("div.card", { class: "extra" });
  assert.equal(el.className, "card extra");
});

test("h() stores html as a string and assigns style objects", () => {
  const el = ui.h("pre", { html: "<b>x</b>", style: { color: "red" } });
  assert.equal(el.innerHTML, "<b>x</b>");
  assert.equal(el.style.color, "red");
});

test("h() wires on* props to listeners that fire with the event type", () => {
  const seen = [];
  const el = ui.h("button.btn", { onclick: (event) => seen.push(event.type) }, "Go");
  el.click();
  assert.deepEqual(seen, ["click"]);
});

test("h() sets known properties as properties and the rest as attributes", () => {
  const input = ui.h("input", { value: "abc" });
  assert.equal(input.value, "abc");
  assert.equal(input.getAttribute("value"), null); // property, not attribute
  const div = ui.h("div", { title: "tip" });
  assert.equal(div.getAttribute("title"), "tip");
});

test("h() writes boolean true as an empty attribute and skips null/false props", () => {
  const btn = ui.h("button.btn", { disabled: true, hidden: false, title: null });
  assert.ok(btn.hasAttribute("disabled"));
  assert.equal(btn.getAttribute("disabled"), "");
  assert.ok(!btn.hasAttribute("hidden"));
  assert.ok(!btn.hasAttribute("title"));
});

test("h() skips null/false/undefined children and flattens arrays", () => {
  const el = ui.h("ul.list", ui.h("li", "one"), null, false, undefined, [ui.h("li", "two")]);
  assert.equal(el.children.length, 2);
  assert.deepEqual(el.children.map((c) => c.textContent), ["one", "two"]);
});

test("h() renders text children and a string props slot as text nodes", () => {
  const el = ui.h("p.note", "hello ", ui.h("b", "world"));
  assert.equal(el.childNodes.length, 2);
  assert.equal(el.textContent, "hello world");
  const plain = ui.h("div", "just text"); // a non-object props slot becomes a child
  assert.equal(plain.textContent, "just text");
});

test("fill() replaces the children in one shot and returns the element", () => {
  const el = ui.h("div.box", ui.h("span", "old"));
  const out = ui.fill(el, "fresh ", ui.h("b", "bold"));
  assert.equal(out, el);
  assert.equal(el.children.length, 1); // the old span is gone; only the new <b> remains
  assert.equal(el.textContent, "fresh bold");
});

test("icon() builds an aria-hidden svg with the named path at the default size", () => {
  const el = ui.icon("check");
  assert.equal(el.tagName, "svg"); // SVG keeps its lowercase name
  assert.equal(el.getAttribute("viewBox"), "0 0 24 24");
  assert.equal(el.getAttribute("width"), "18");
  assert.equal(el.getAttribute("height"), "18");
  assert.equal(el.getAttribute("aria-hidden"), "true");
  assert.equal(el.getAttribute("stroke"), "currentColor");
  const path = el.children[0];
  assert.equal(path.tagName, "path");
  assert.equal(path.getAttribute("d"), "m5 13 4.5 4.5L19 7");
});

test("icon() honours a custom size", () => {
  const el = ui.icon("check", 16);
  assert.equal(el.getAttribute("width"), "16");
  assert.equal(el.getAttribute("height"), "16");
});

test("icon() falls back to the info path for an unknown name", () => {
  const el = ui.icon("no-such-icon");
  assert.equal(el.children[0].getAttribute("d"), "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18M12 11v5M12 8h.01");
});

test("badge() shows the prettified text with a tone from its status", () => {
  const el = ui.badge("completed");
  assert.equal(el.tagName, "SPAN");
  assert.ok(el.classList.contains("badge"));
  assert.equal(el.getAttribute("data-tone"), "success");
  assert.equal(el.textContent, "Completed");
});

test("badge() takes an explicit tone and prettifies identifiers", () => {
  const el = ui.badge("in_progress", "info");
  assert.equal(el.getAttribute("data-tone"), "info");
  assert.equal(el.textContent, "In Progress");
});

test("statusBadge() maps the status to a tone without a live dot", () => {
  const el = ui.statusBadge("running");
  assert.ok(el.classList.contains("badge"));
  assert.equal(el.getAttribute("data-tone"), "info");
  assert.equal(el.children.length, 0); // no dot when not live
  assert.equal(el.textContent, "Running");
});

test("statusBadge() adds the pulsing dot when live", () => {
  const el = ui.statusBadge("running", true);
  const dot = el.children[0];
  assert.equal(dot.tagName, "SPAN");
  assert.equal(dot.className, "dot dot-live");
  assert.equal(el.textContent, "Running");
});
