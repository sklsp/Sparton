// The split-flap board builders from board.js on the DOM fake: flapWord, flapTo, strip and
// boardRow. Checked against the file's comments: one aria-label per row (the real value, not
// tile soup), mark/struck options, blank tiles for empty values.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import { setMedia } from "./dom.mjs"; // ...then the DOM fake (document and matchMedia).
import * as board from "../../app/web/board.js";

test("flapWord builds one tile per character with a single aria-label", () => {
  const el = board.flapWord("AB-1");
  assert.equal(el.tagName, "SPAN");
  assert.ok(el.classList.contains("flaps"));
  assert.equal(el.getAttribute("role"), "img");
  assert.equal(el.getAttribute("aria-label"), "AB-1"); // the real value, not letter by letter
  assert.deepEqual([...el.children].map((t) => t.textContent), ["A", "B", "-", "1"]);
});

test("flapWord marks blank tiles with a no-break space and honours className", () => {
  const el = board.flapWord("A B", { className: "cell-c" });
  assert.equal(el.className, "flaps cell-c");
  const blank = el.children[1];
  assert.ok(blank.classList.contains("flap"));
  assert.equal(blank.textContent, "\u00a0"); // no-break space keeps the tile's width
  assert.equal(blank.dataset.blank, "");
  assert.equal(el.children[0].dataset.blank, undefined); // filled tiles carry no blank mark
});

test("flapWord defaults the label to the text but takes an override", () => {
  assert.equal(board.flapWord("X9").getAttribute("aria-label"), "X9");
  const el = board.flapWord("X9", { label: "Price X9" });
  assert.equal(el.getAttribute("aria-label"), "Price X9");
});

test("strip wraps its text in a strip-text span", () => {
  const s = board.strip("ACME", "cell-a");
  assert.ok(s.classList.contains("strip"));
  assert.ok(s.classList.contains("cell-a"));
  assert.equal(s.children.length, 1);
  assert.ok(s.children[0].classList.contains("strip-text"));
  assert.equal(s.children[0].textContent, "ACME");
});

test("flapTo turns each tile to the target and keeps the aria-label on the row", async () => {
  const wrap = board.flapWord("AB");
  await board.flapTo(wrap, "CD"); // resolves when all tiles rest
  assert.equal([...wrap.children].map((t) => t.textContent).join(""), "CD");
  assert.equal(wrap.getAttribute("aria-label"), "CD");
});

test("flapTo pads with blank tiles and trims extras", async () => {
  const grow = board.flapWord("A");
  await board.flapTo(grow, "AB");
  assert.equal(grow.children.length, 2);
  assert.equal([...grow.children].map((t) => t.textContent).join(""), "AB");

  const shrink = board.flapWord("ABC");
  await board.flapTo(shrink, "A");
  assert.equal(shrink.children.length, 1);
  assert.equal(shrink.getAttribute("aria-label"), "A");
});

test("flapTo sets the tiles at once when reduced motion is on", async () => {
  setMedia("(prefers-reduced-motion: reduce)", true);
  try {
    const wrap = board.flapWord("XY");
    await board.flapTo(wrap, "ZQ");
    assert.equal([...wrap.children].map((t) => t.textContent).join(""), "ZQ");
    assert.equal(wrap.getAttribute("aria-label"), "ZQ");
  } finally {
    setMedia("(prefers-reduced-motion: reduce)", false);
  }
});

test("boardRow reads as one sentence and hides the tiles from assistive tech", () => {
  const li = board.boardRow({ a: "ACME", b: "Widget Pro", c: "12", d: "9", e: "8" });
  assert.equal(li.tagName, "LI");
  assert.ok(li.classList.contains("board-row"));
  assert.ok(li.classList.contains("mark-row-exact")); // the default mark
  const say = li.querySelector(".row-say");
  assert.ok(say.classList.contains("visually-hidden"));
  assert.equal(say.textContent, "ACME, Widget Pro, 12, 9, 8");
  const cells = li.querySelector(".board-cells");
  assert.equal(cells.getAttribute("aria-hidden"), "true"); // the tiles are decoration for AT
});

test("boardRow honours the mark, struck and tone options", () => {
  const li = board.boardRow({ a: "A", b: "B", c: "1", d: "2", e: "3", mark: "found", struck: true, tone: "danger" });
  assert.ok(li.classList.contains("mark-row-found"));
  assert.ok(!li.classList.contains("mark-row-exact"));
  assert.ok(li.classList.contains("is-struck")); // sold out
  assert.equal(li.getAttribute("data-tone"), "danger");
});

test("boardRow puts a span strip across the last two columns instead of figures", () => {
  const li = board.boardRow({ a: "A", b: "B", c: "1", d: "2", e: "3", span: "SOLD OUT" });
  assert.equal(li.getAttribute("data-span"), "1");
  const cells = li.querySelector(".board-cells");
  assert.equal(cells.querySelector(".cell-d"), null);
  assert.equal(cells.querySelector(".cell-e"), null);
  const spanStrip = cells.querySelector(".cell-span");
  assert.ok(spanStrip.classList.contains("strip"));
  assert.equal(spanStrip.children[0].textContent, "SOLD OUT");
});

test("boardRow fills empty values with blank tiles and skips them in the sentence", () => {
  const li = board.boardRow({ a: "ACME", b: "Widget" }); // c, d, e stay ""
  assert.equal(li.querySelector(".row-say").textContent, "ACME, Widget");
  const figC = li.querySelector(".cell-c");
  assert.equal(figC.children.length, 1);
  assert.equal(figC.children[0].dataset.blank, ""); // one blank tile keeps the column's width
  assert.equal(figC.getAttribute("aria-label"), null); // figures do not announce; row-say does
  assert.equal(figC.getAttribute("role"), null);
});

test("boardRow lets say override the sentence and shows note under the name", () => {
  const li = board.boardRow({ a: "A", b: "B", c: "1", d: "2", e: "3", say: "Custom sentence", note: "Below owner price" });
  assert.equal(li.querySelector(".row-say").textContent, "Custom sentence. Below owner price");
  const name = li.querySelector(".cell-b");
  assert.equal(name.querySelector(".strip-note").textContent, "Below owner price");
});

test("boardRow appends extractedNote only for the extracted mark", () => {
  const li = board.boardRow({ a: "A", b: "B", c: "1", d: "2", e: "3", mark: "extracted", extractedNote: "from listing" });
  assert.equal(li.querySelector(".row-say").textContent, "A, B, 1, 2, 3 (from listing)");
  const other = board.boardRow({ a: "A", b: "B", c: "1", d: "2", e: "3", mark: "exact", extractedNote: "ignored" });
  assert.equal(other.querySelector(".row-say").textContent, "A, B, 1, 2, 3");
});

