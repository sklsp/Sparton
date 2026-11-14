// translateDom and langSwitch from i18n.js on the DOM fake: which elements get filled, how
// attributes are set, and that exactly one language is marked current in the switch.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake (document).
import * as i18n from "../../app/web/i18n.js";

test("translateDom fills elements marked with data-i18n", () => {
  const root = document.createElement("div");
  const a = document.createElement("span");
  a.setAttribute("data-i18n", "nav.overview");
  const b = document.createElement("p");
  b.textContent = "stale";
  b.setAttribute("data-i18n", "plan.perMonth");
  root.append(a, b);
  i18n.translateDom(root);
  assert.equal(a.textContent, i18n.t("nav.overview")); // in Node: the key itself (empty dict)
  assert.equal(b.textContent, i18n.t("plan.perMonth")); // replaces stale text
});

test("translateDom fills data-i18n-html and sets attributes from data-i18n-attr", () => {
  const root = document.createElement("div");
  const h = document.createElement("p");
  h.setAttribute("data-i18n-html", "plan.weeklyReport");
  const inp = document.createElement("input");
  inp.setAttribute("data-i18n-attr", "placeholder:nav.search;aria-label:nav.searchLabel");
  root.append(h, inp);
  i18n.translateDom(root);
  assert.equal(h.innerHTML, i18n.t("plan.weeklyReport"));
  assert.equal(inp.getAttribute("placeholder"), i18n.t("nav.search"));
  assert.equal(inp.getAttribute("aria-label"), i18n.t("nav.searchLabel"));
});

test("translateDom leaves unmarked elements alone", () => {
  const root = document.createElement("div");
  const keep = document.createElement("span");
  keep.textContent = "untouched";
  root.append(keep);
  i18n.translateDom(root);
  assert.equal(keep.textContent, "untouched");
});

test("langSwitch renders both languages with the current one marked", () => {
  const wrap = i18n.langSwitch();
  assert.ok(wrap.classList.contains("lang-switch"));
  assert.equal(wrap.getAttribute("role"), "group");
  assert.deepEqual([...wrap.children].map((b) => b.textContent), ["NL", "EN"]); // LANGS order
  const [nl, en] = [...wrap.children];
  assert.equal(nl.lang, "nl");
  assert.equal(en.lang, "en");
  // The accessible name contains the visible text (WCAG 2.5.3): "NL, Nederlands".
  assert.equal(nl.getAttribute("aria-label"), `NL, ${i18n.t("lang.nl")}`);
  const pressed = [...wrap.children].filter((b) => b.getAttribute("aria-pressed") === "true");
  assert.equal(pressed.length, 1); // exactly one current language
  assert.equal(pressed[0].lang, i18n.lang); // whichever pick() chose in Node
});
