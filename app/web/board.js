// The split-flap board: one tile per character, turned in whole steps like the real thing.
// No tweening: a tile shows a character or the next one, never something in between.

const DRUM = " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789€.,-−+%/:";
const STEP_MS = 55;
const reduced = () => matchMedia("(prefers-reduced-motion: reduce)").matches;

function tile(ch) {
  const el = document.createElement("span");
  el.className = "flap";
  setChar(el, ch);
  return el;
}

function setChar(el, ch) {
  el.textContent = ch === " " ? " " : ch;
  if (ch === " ") el.dataset.blank = ""; else delete el.dataset.blank;
}

/** A static row of tiles. The text is exposed once to assistive tech, not letter by letter. */
export function flapWord(text, { label = text, className = "" } = {}) {
  const wrap = document.createElement("span");
  wrap.className = `flaps ${className}`.trim();
  wrap.setAttribute("role", "img");
  wrap.setAttribute("aria-label", label);
  for (const ch of String(text)) wrap.append(tile(ch));
  return wrap;
}

/**
 * Turn a tile row to `text`. Each tile walks the drum from its current character to the target,
 * one whole step at a time, starting a step after its left neighbour. Resolves when all tiles rest.
 */
export function flapTo(wrap, text, { label = text } = {}) {
  const target = String(text).toUpperCase();
  while (wrap.children.length < target.length) wrap.append(tile(" "));
  while (wrap.children.length > target.length) wrap.lastChild.remove();
  wrap.setAttribute("aria-label", label);
  const tiles = [...wrap.children];

  if (reduced()) {
    tiles.forEach((el, i) => setChar(el, target[i]));
    return Promise.resolve();
  }

  return Promise.all(tiles.map((el, i) => new Promise((done) => {
    const goal = target[i];
    let pos = Math.max(0, DRUM.indexOf((el.textContent || " ").replace(" ", " ")));
    const end = DRUM.indexOf(goal);
    if (end < 0 || DRUM[pos] === goal) { setChar(el, goal); return done(); }
    // Bound the walk so a long drum never stalls the row: at most 8 steps per tile.
    const steps = Math.min(8, (end - pos + DRUM.length) % DRUM.length);
    pos = (end - steps + DRUM.length) % DRUM.length;
    let n = 0;
    const tick = () => {
      n += 1;
      setChar(el, n >= steps ? goal : DRUM[(pos + n) % DRUM.length]);
      el.classList.remove("turning");
      void el.offsetWidth; // restart the step animation
      el.classList.add("turning");
      if (n >= steps) return done();
      setTimeout(tick, STEP_MS);
    };
    setTimeout(tick, i * STEP_MS);
  })));
}

/* ------------------------------------------------------------- board rows */
// Five cells: two name strips (whole-word flaps, like destination strips on a station board)
// and three character-flap figures. `mark` is the certainty of the row; `struck` = sold out.
// A status word (SOLD OUT, FOUND, EXACT) is too long for one figure, so it gets a strip across
// the last two columns, like the CANCELLED strip on a departure board.
// Screen readers get one sentence per row (`say`, or the cells joined), not tile soup.

const node = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

export function strip(text, cls = "") {
  const s = node("span", `strip ${cls}`.trim());
  s.append(node("span", "strip-text", text));
  return s;
}

function figure(text, cls) {
  const w = flapWord(text || " ", { className: `fig ${cls}` });
  w.removeAttribute("role");
  w.removeAttribute("aria-label");
  return w;
}

export function boardRow({ a, b, c = "", d = "", e = "", span = "", mark = "exact", struck = false, tone = "", say, extractedNote = "" }, tag = "li") {
  const li = node(tag, `board-row mark-row-${mark}`);
  li.dataset.tone = tone;
  li.dataset.span = span ? "1" : "";
  if (struck) li.classList.add("is-struck");
  const cells = node("span", "board-cells");
  cells.setAttribute("aria-hidden", "true");
  cells.append(strip(a, "cell-a"), strip(b, "cell-b"), figure(c, "cell-c"));
  if (span) cells.append(strip(span, "cell-span"));
  else cells.append(figure(d, "cell-d"), figure(e, "cell-e"));
  const sentence = say || [a, b, c, d, e, span].filter(Boolean).join(", ");
  li.append(node("span", "visually-hidden row-say", sentence + (mark === "extracted" && extractedNote ? ` (${extractedNote})` : "")), cells);
  return li;
}
