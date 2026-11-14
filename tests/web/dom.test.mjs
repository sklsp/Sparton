// The DOM fake in dom.mjs behaves like the browser for what app/web's builders call:
// tree building, textContent, classList, attributes and dataset, events and selectors.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // env's fakes first, as in the real usage order
import { Event, resetDom, setMedia } from "./dom.mjs";

test("document exposes the entry points the builders use", () => {
  assert.equal(typeof document.createElement, "function");
  assert.equal(typeof document.createTextNode, "function");
  assert.equal(typeof document.createElementNS, "function");
  assert.ok(document.body);
  assert.ok(document.documentElement);
});

test("append and appendChild build a tree; children holds elements only", () => {
  const div = document.createElement("div");
  const span = document.createElement("span");
  div.append(span, "hi"); // strings become text nodes, as in the browser
  assert.equal(div.childNodes.length, 2);
  assert.deepEqual(div.children.map((n) => n.tagName), ["SPAN"]);
  assert.equal(div.lastChild.nodeType, 3);
  const returned = div.appendChild(document.createElement("i"));
  assert.equal(returned.tagName, "I"); // appendChild returns the node (toast relies on it)
  assert.equal(div.children.length, 2);
});

test("replaceChildren clears and refills in one shot", () => {
  const div = document.createElement("div");
  div.append(document.createElement("span"), "old");
  div.replaceChildren(); // fill() calls it with no arguments
  assert.equal(div.childNodes.length, 0);
  div.replaceChildren("new");
  assert.equal(div.textContent, "new");
});

test("textContent walks the whole tree in order", () => {
  const outer = document.createElement("div");
  const first = document.createElement("span");
  first.textContent = "ab";
  const second = document.createElement("em");
  second.append(document.createTextNode("d"));
  outer.append(first, document.createTextNode("c"), second);
  assert.equal(outer.textContent, "abcd");
});

test("setting textContent replaces the children with one text node", () => {
  const span = document.createElement("span");
  span.append(document.createElement("i"), "x");
  span.textContent = "y"; // board.js setChar pattern
  assert.equal(span.childNodes.length, 1);
  assert.equal(span.lastChild.nodeType, 3);
  assert.equal(span.textContent, "y");
});

test("classList and className stay in sync", () => {
  const el = document.createElement("div");
  el.className = "flap"; // board.js tile pattern
  assert.ok(el.classList.contains("flap"));
  el.classList.add("turning");
  assert.equal(el.className, "flap turning");
  el.classList.remove("turning");
  assert.equal(el.className, "flap");
  assert.equal(el.classList.toggle("on"), true);
  assert.equal(el.classList.toggle("on"), false);
  assert.ok(!el.classList.contains("on"));
});

test("className += keeps the existing classes (h() pattern)", () => {
  const el = document.createElement("button");
  el.className = "btn";
  el.className += " primary"; // what h() does for a class prop
  assert.equal(el.className, "btn primary");
});

test("setAttribute stores strings; getAttribute returns null when absent", () => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); // icon() pattern
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", 18); // numbers are coerced like in the browser
  assert.equal(svg.getAttribute("width"), "18");
  assert.ok(!svg.hasAttribute("aria-hidden"));
  svg.setAttribute("aria-hidden", "true");
  assert.ok(svg.hasAttribute("aria-hidden"));
  svg.removeAttribute("viewBox");
  assert.equal(svg.getAttribute("viewBox"), null);
});

test("dataset maps camelCase to data- attributes", () => {
  const el = document.createElement("span");
  el.dataset.i18nAttr = "aria-label:nav.overview"; // translateDom reads this back
  assert.equal(el.getAttribute("data-i18n-attr"), "aria-label:nav.overview");
  assert.equal(el.dataset.i18nAttr, "aria-label:nav.overview");
  el.dataset.blank = ""; // board.js blank tile pattern
  assert.ok(el.hasAttribute("data-blank"));
  delete el.dataset.blank;
  assert.ok(!el.hasAttribute("data-blank"));
  assert.equal(el.dataset.neverSet, undefined);
});

test("appendChild rejects values that are not nodes", () => {
  const el = document.createElement("div");
  assert.throws(() => el.appendChild(42), TypeError);
});

test("addEventListener and dispatchEvent call listeners in order; click() drives them", () => {
  const btn = document.createElement("button");
  const seen = [];
  btn.addEventListener("click", () => seen.push(1));
  btn.addEventListener("click", () => seen.push(2));
  btn.click(); // langSwitch buttons are driven this way
  assert.deepEqual(seen, [1, 2]);
});

test("the once option removes the listener after one dispatch (toast pattern)", () => {
  const el = document.createElement("div");
  let calls = 0;
  el.addEventListener("animationend", () => calls += 1, { once: true });
  el.dispatchEvent(new Event("animationend"));
  el.dispatchEvent(new Event("animationend"));
  assert.equal(calls, 1);
});

test("preventDefault makes dispatchEvent return false", () => {
  const el = document.createElement("div");
  el.addEventListener("click", (e) => e.preventDefault());
  assert.equal(el.click(), false);
  const quiet = document.createElement("div");
  quiet.addEventListener("click", () => {});
  assert.equal(quiet.click(), true);
});

test("querySelectorAll matches tag, class, id and attribute selectors", () => {
  const root = document.createElement("div");
  const a = document.createElement("span");
  a.className = "toast-close";
  a.id = "close-btn";
  a.setAttribute("data-tone", "danger");
  const b = document.createElement("span");
  b.className = "toast-icon";
  b.setAttribute("data-tone", "info");
  const c = document.createElement("div");
  c.className = "card toast";
  root.append(a, b, c);

  assert.equal(root.querySelectorAll("span").length, 2);
  assert.equal(root.querySelector(".toast-close"), a);
  assert.equal(root.querySelector("#close-btn"), a);
  assert.deepEqual(root.querySelectorAll("[data-tone]"), [a, b]);
  assert.deepEqual(root.querySelectorAll('[data-tone="danger"]'), [a]);
  assert.deepEqual(root.querySelectorAll("div.card"), [c]); // compound tag.class
  assert.equal(root.querySelector(".nope"), null);
});

test("document.querySelectorAll searches the whole tree from <html>", () => {
  resetDom();
  const p = document.createElement("p");
  p.setAttribute("data-i18n", "nav.overview"); // translateDom pattern
  document.body.append(p);
  assert.deepEqual(document.querySelectorAll("[data-i18n]"), [p]);
  assert.equal(document.querySelector("[data-i18n]").dataset.i18n, "nav.overview");
});

test("innerHTML stores the string and style is a plain object", () => {
  const el = document.createElement("div");
  el.innerHTML = "<b>bold</b>"; // h() html prop and translateDom data-i18n-html
  assert.equal(el.innerHTML, "<b>bold</b>");
  Object.assign(el.style, { width: "50%" }); // h() style prop pattern
  assert.equal(el.style.width, "50%");
});

test("remove detaches the node from its parent (flapTo trims tiles this way)", () => {
  const wrap = document.createElement("span");
  const t1 = document.createElement("span");
  const t2 = document.createElement("span");
  wrap.append(t1, t2);
  wrap.lastChild.remove();
  assert.equal(wrap.children.length, 1);
  assert.equal(wrap.lastChild, t1);
});

test("createElementNS keeps the SVG tag lowercase; HTML tags are uppercase", () => {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M3 10.5 12 3l9 7.5");
  svg.append(path);
  assert.equal(svg.tagName, "svg");
  assert.equal(document.createElement("div").tagName, "DIV");
});

test("matchMedia reports false until setMedia flips it (board.js reduced-motion check)", () => {
  resetDom();
  const query = "(prefers-reduced-motion: reduce)";
  assert.equal(matchMedia(query).matches, false);
  setMedia(query, true);
  assert.equal(matchMedia(query).matches, true);
});

test("resetDom empties the body and clears lang", () => {
  document.body.append(document.createElement("div"));
  document.documentElement.lang = "nl"; // i18n.js sets this at import
  resetDom();
  assert.equal(document.body.childNodes.length, 0);
  assert.equal(document.documentElement.lang, "");
});

test("querySelectorAll refuses combinators instead of silently mis-matching", () => {
  const root = document.createElement("ul");
  root.className = "plan-features";
  root.append(document.createElement("li"));
  assert.throws(() => root.querySelectorAll(".plan-features li"), /compound selectors only/);
  assert.throws(() => root.querySelector("ul > li"), /compound selectors only/);
  assert.equal(root.querySelectorAll('[data-x="a b"]').length, 0); // spaces inside an attribute value are fine
});
