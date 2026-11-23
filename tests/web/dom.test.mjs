// The DOM fake in dom.mjs behaves like the browser for what app/web's builders call:
// tree building, textContent, classList, attributes and dataset, events and selectors.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // env's fakes first, as in the real usage order
import { Event, resetDom, setMedia, setHidden } from "./dom.mjs";

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

test("a directly assigned on<type> property fires on dispatch, as in the browser", () => {
  const btn = document.createElement("button"); // system.js assigns refresh.onclick after h() built it
  let currentTarget;
  btn.onclick = (event) => { currentTarget = event.currentTarget; };
  btn.click();
  assert.equal(currentTarget, btn);
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

test("document.hidden stays false until setHidden flips it; resetDom restores it (agent.js poll pause)", () => {
  resetDom();
  assert.equal(document.hidden, false);
  setHidden(true);
  assert.equal(document.hidden, true);
  resetDom();
  assert.equal(document.hidden, false);
});

test("resetDom empties the body and clears lang", () => {
  document.body.append(document.createElement("div"));
  document.documentElement.lang = "nl"; // i18n.js sets this at import
  resetDom();
  assert.equal(document.body.childNodes.length, 0);
  assert.equal(document.documentElement.lang, "");
});

test("descendant selectors match inside the queried root; child and sibling combinators are refused", () => {
  const root = document.createElement("section");
  root.className = "plan-features";
  const ul = document.createElement("ul");
  const li1 = document.createElement("li");
  const li2 = document.createElement("li"); li2.className = "on";
  ul.append(li1, li2);
  root.append(ul);
  assert.deepEqual(root.querySelectorAll("ul li"), [li1, li2]);
  assert.deepEqual(root.querySelectorAll("ul li.on"), [li2]);
  assert.deepEqual(root.querySelectorAll(".plan-features li"), []); // the root itself is not its own ancestor here
  assert.throws(() => root.querySelectorAll("ul > li"), /descendants only/);
  assert.throws(() => root.querySelector("li + li"), /descendants only/);
  assert.equal(root.querySelectorAll('[data-x="a b"]').length, 0); // spaces inside an attribute value are fine
});

test("prepend inserts nodes before the first child, in order", () => {
  const ul = document.createElement("ul");
  const keep = document.createElement("li");
  ul.append(keep);
  const a = document.createElement("li");
  const b = document.createElement("li");
  ul.prepend(a, b);
  assert.deepEqual([...ul.children], [a, b, keep]); // order kept, old child last

  const moved = document.createElement("div");
  moved.append(keep); // a node with a parent moves instead of duplicating; ul is down to two
  ul.prepend(moved);
  assert.deepEqual([...ul.children], [moved, a, b]);
  assert.equal(moved.children.length, 1); // keep came along inside it

  ul.prepend("hello"); // strings become text nodes like appendChild; prepend itself is void
  assert.equal(ul.childNodes[0].nodeType, 3);
  assert.equal(ul.childNodes[0].textContent, "hello");
  assert.throws(() => ul.prepend(42), /prepend: not a node/);
});

test("dialog opens with showModal; close sets returnValue and fires the close event (ui.js dialog)", () => {
  const el = document.createElement("dialog"); // ui.js dialog() pattern
  let closedWith;
  el.addEventListener("close", () => { closedWith = el.returnValue; });
  el.showModal();
  assert.equal(el.open, true);
  el.close("ok");
  assert.equal(el.open, false);
  assert.equal(closedWith, "ok"); // the close handler reads returnValue synchronously
  const bare = document.createElement("dialog");
  bare.close(); // no value: returnValue stays undefined
  assert.equal(bare.returnValue, undefined);
});

test("dispatchEvent sets event.target like the browser (settings.js reads e.target)", () => {
  const el = document.createElement("form");
  let target;
  el.addEventListener("submit", (e) => { target = e.target; });
  el.dispatchEvent(new Event("submit"));
  assert.equal(target, el);
});

test("reset clears text inputs and leaves checkboxes alone (settings.js password form)", () => {
  const form = document.createElement("form");
  const cur = document.createElement("input");
  cur.type = "password";
  const box = document.createElement("input");
  box.type = "checkbox";
  form.append(cur, box);
  cur.value = "secret";
  box.checked = true;
  form.reset();
  assert.equal(cur.value, "");
  assert.equal(box.checked, true); // browsers keep checkboxes at their default state
});

test("a comma list matches any of its parts, focus tracks activeElement, toggleAttribute flips", () => {
  const list = document.createElement("ul");
  const a = document.createElement("li"); a.className = "ob-searching";
  const b = document.createElement("li"); b.className = "ob-none";
  const c = document.createElement("li");
  list.append(a, b, c);
  assert.deepEqual(list.querySelectorAll(".ob-searching, .ob-none"), [a, b]);
  assert.equal(document.activeElement, document.body); // nothing focused yet
  const input = document.createElement("input");
  input.focus();
  assert.equal(document.activeElement, input);
  assert.equal(c.toggleAttribute("data-done"), true);
  assert.equal(c.hasAttribute("data-done"), true);
  assert.equal(c.toggleAttribute("data-done"), false);
  assert.equal(c.toggleAttribute("data-done", false), false);
  assert.equal(c.hasAttribute("data-done"), false);
});

test("lastElementChild skips text nodes; scrollIntoView is a no-op (agent.js timeline)", () => {
  const div = document.createElement("div");
  assert.equal(div.lastElementChild, null); // an empty element has none
  const span = document.createElement("span");
  span.textContent = "x";
  div.append(span, "trailing text");
  assert.equal(div.lastElementChild, span); // the last child is a text node; elements only
  let threw = false;
  try {
    div.scrollIntoView({ block: "nearest" }); // agent.js passes options; the fake ignores them
  } catch {
    threw = true;
  }
  assert.equal(threw, false);
});

test("replaceWith takes the node's place in its parent, in order (agent.js tab strip)", () => {
  const parent = document.createElement("div");
  const before = document.createElement("span");
  const placeholder = document.createElement("i");
  const after = document.createElement("b");
  parent.append(before, placeholder, after);

  const a = document.createElement("em");
  const b = document.createElement("strong");
  placeholder.replaceWith(a, "text", b); // strings become text nodes, like the browser

  assert.deepEqual([...parent.children].map((el) => el.tagName), ["SPAN", "EM", "STRONG", "B"]); // in order, at its place
  assert.equal(parent.textContent, "text");
  assert.ok(![...parent.childNodes].includes(placeholder)); // the replaced node is out of the tree
});

test("requestSubmit dispatches the form's submit event (agent.js Ctrl+Enter)", () => {
  const form = document.createElement("form");
  let submitted = 0;
  form.addEventListener("submit", (event) => { event.preventDefault(); submitted += 1; });
  assert.equal(form.requestSubmit(), false); // preventDefault makes dispatchEvent return false, as in the browser
  assert.equal(submitted, 1);
});

test("checked reflects the checked attribute both ways (settings.js reads box.checked)", () => {
  const box = document.createElement("input");
  assert.equal(box.checked, false); // no attribute yet
  box.checked = true;
  assert.ok(box.hasAttribute("checked")); // h() writes boolean props as attributes; reading back must agree
  assert.equal(box.checked, true);
  box.checked = false;
  assert.equal(box.hasAttribute("checked"), false);
});

test("parentElement is the nearest element ancestor or null (catalog.js clears sibling aria-sort)", () => {
  const row = document.createElement("tr");
  const cell = document.createElement("th");
  row.append(cell);
  assert.equal(cell.parentElement, row);
  assert.equal(row.parentElement, null); // detached: nothing above it
});
