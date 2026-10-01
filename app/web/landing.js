// Landing page. The copy is static HTML (readable without JavaScript); this module translates it,
// runs the demo board, drives the scroll story and prices the plans from the API, so the page
// can never advertise a price the server does not enforce.

import { t, translateDom, langSwitch, fmtMoney } from "/app/i18n.js";
import { flapTo, boardRow as row } from "/app/board.js";
import { planBoard } from "/app/plans.js";

translateDom();
document.getElementById("lp-tools")?.prepend(langSwitch());

const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
// anime.js only drives the entrances, so it loads after first paint and never under reduced motion.
const anime = reduced ? null : import("/app/vendor/anime.esm.js");
let animeModule = null;
anime?.then((m) => { animeModule = m; });
// Rows that are on screen before anime.js arrives simply stay put: no flash from visible to 0.
const animate = (...args) => animeModule?.animate(...args);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};
const eur = (v) => fmtMoney(v, "EUR");
const pct = (was, now) => {
  const p = Math.round(((now - was) / was) * 100);
  return `${p > 0 ? "+" : "−"}${Math.abs(p)}%`;
};

/* ------------------------------------------------------------ demo data */
// Invented shops and products, labelled "Demo data" on every board. Shaped like real Sparton output.
const SHOPS = { kade: "Kade & Co", noord: "Noord Supply", vos: "Atelier Vos" };
const P = {
  belt: t("lp.demo.belt"), oil: t("lp.demo.oil"), scarf: t("lp.demo.scarf"),
  bag: t("lp.demo.bag"), socks: t("lp.demo.socks"), wallet: t("lp.demo.wallet"),
};

function setStrip(s, text) {
  const inner = s.querySelector(".strip-text");
  if (inner.textContent === text) return;
  if (reduced) { inner.textContent = text; return; }
  s.classList.remove("turning");
  void s.offsetWidth;
  s.classList.add("turning");
  setTimeout(() => { inner.textContent = text; }, 55);
}

const withNote = (r) => ({ ...r, extractedNote: t("lp.legend.extractedShort") });

/** Bring an existing <li> to a new state, flap by flap. */
function turnRow(li, next) {
  if (li.dataset.span !== (next.span ? "1" : "")) { li.replaceWith(row(withNote(next))); return; }
  const [a, b, c, d, e] = li.querySelector(".board-cells").children;
  li.querySelector(".row-say").textContent = [next.a, next.b, next.c, next.d, next.e, next.span].filter(Boolean).join(", ");
  if (next.span) {
    setStrip(a, next.a); setStrip(b, next.b); flapTo(c, next.c || " "); setStrip(d, next.span);
    li.className = `board-row mark-row-${next.mark || "exact"}${next.struck ? " is-struck" : ""}`;
    li.dataset.tone = next.tone || "";
    return;
  }
  setStrip(a, next.a);
  setStrip(b, next.b);
  flapTo(c, next.c || " ");
  flapTo(d, next.d || " ");
  flapTo(e, next.e || " ");
  li.className = `board-row mark-row-${next.mark || "exact"}${next.struck ? " is-struck" : ""}`;
  li.dataset.tone = next.tone || "";
}

/** Show `rows` on a board, turning the rows that exist and adding/removing the rest. */
function showRows(list, rows) {
  const items = [...list.children];
  rows.forEach((r, i) => {
    if (items[i]) turnRow(items[i], r);
    else {
      const li = row(withNote(r));
      list.append(li);
      if (!reduced) animate(li, { opacity: [0, 1], translateY: [-8, 0], duration: 360, delay: i * 70, ease: "steps(4)" });
    }
  });
  items.slice(rows.length).forEach((li) => li.remove());
}

/* -------------------------------------------------------- the hero board */
const change = (shop, product, was, now, extra = {}) =>
  ({ a: shop, b: product, c: eur(was), d: eur(now), e: pct(was, now), tone: now < was ? "down" : "up", ...extra });
const soldOut = (shop, product, now, extra = {}) =>
  ({ a: shop, b: product, c: eur(now), span: t("lp.demo.soldout"), struck: true, tone: "stock", ...extra });

const WEEK = [
  change(SHOPS.kade, P.belt, 34, 27.5),
  change(SHOPS.noord, P.oil, 18.95, 21.5),
  soldOut(SHOPS.vos, P.scarf, 59),
  change(SHOPS.kade, P.bag, 129, 99, { mark: "extracted" }),
  change(SHOPS.noord, P.socks, 14.95, 12.5),
  { a: SHOPS.vos, b: P.wallet, c: eur(45), span: t("lp.demo.new"), tone: "new" },
];
// The attract loop: the same week, a few rows replaced one at a time, then back.
const LATER = [
  [0, change(SHOPS.kade, P.belt, 27.5, 32)],
  [2, { a: SHOPS.vos, b: P.scarf, c: eur(59), span: t("lp.demo.back"), tone: "new" }],
  [4, change(SHOPS.noord, P.socks, 12.5, 9.95)],
  [1, change(SHOPS.noord, P.oil, 21.5, 19.95)],
];

const heroRows = document.getElementById("hero-rows");
if (heroRows) {
  showRows(heroRows, WEEK);
  if (!reduced) {
    let i = 0;
    let state = WEEK.slice();
    const tick = () => {
      if (document.hidden) return;
      if (i < LATER.length) {
        const [idx, r] = LATER[i];
        state[idx] = r;
      } else {
        state = WEEK.slice();
        i = -1;
      }
      i += 1;
      showRows(heroRows, state);
    };
    let timer = null;
    // Only flip while the board is on screen: an off-screen attract loop is wasted work.
    new IntersectionObserver(([entry]) => {
      clearInterval(timer);
      if (entry.isIntersecting) timer = setInterval(tick, 3200);
    }).observe(heroRows);
  }
}

/* ------------------------------------------------------- the scroll story */
const SCENES = {
  shop: {
    title: t("lp.scene.shop"),
    rows: [
      { a: t("lp.demo.k.shop"), b: "jouwwinkel.nl" },
      { a: t("lp.demo.k.platform"), b: "Shopify" },
      { a: t("lp.demo.k.sells"), b: t("lp.demo.k.sellsVal") },
      { a: t("lp.demo.k.search"), b: "", span: t("lp.demo.searching"), tone: "new" },
    ],
    foot: t("lp.scene.shop.foot"),
  },
  discover: {
    title: t("lp.scene.discover"),
    rows: [
      { a: SHOPS.kade, b: "Shopify", span: t("lp.demo.found"), tone: "new" },
      { a: SHOPS.noord, b: "WooCommerce", span: t("lp.demo.found"), tone: "new" },
      { a: SHOPS.vos, b: "Lightspeed", span: t("lp.demo.found"), mark: "extracted", tone: "new" },
      { a: "Mannenwerk", b: "Shopify", span: t("lp.demo.skipped"), struck: true },
    ],
    foot: t("lp.scene.discover.foot"),
  },
  match: {
    title: t("lp.scene.match"),
    rows: [
      { a: SHOPS.kade, b: P.belt, c: eur(34), span: t("lp.demo.inStock") },
      { a: SHOPS.kade, b: P.bag, c: eur(129), span: t("lp.demo.inStock"), mark: "extracted" },
      { a: SHOPS.noord, b: P.oil, c: eur(18.95), span: t("lp.demo.inStock") },
      { a: SHOPS.noord, b: P.socks, c: eur(14.95), span: t("lp.demo.inStock") },
      { a: SHOPS.vos, b: P.scarf, c: eur(59), span: t("lp.demo.inStock"), mark: "extracted" },
    ],
    foot: t("lp.scene.match.foot"),
  },
  moves: { title: t("lp.scene.moves"), rows: WEEK, foot: t("lp.scene.moves.foot") },
  certainty: {
    title: t("lp.scene.certainty"),
    rows: [
      { ...WEEK[0], c: WEEK[0].d, span: t("lp.demo.exact") },
      { ...WEEK[1], c: WEEK[1].d, span: t("lp.demo.exact") },
      { ...WEEK[3], c: WEEK[3].d, span: t("lp.demo.extracted") },
      { ...WEEK[4], c: WEEK[4].d, span: t("lp.demo.exact") },
    ],
    foot: t("lp.scene.certainty.foot"),
  },
  report: {
    title: t("lp.scene.report"),
    rows: [
      { a: SHOPS.kade, b: t("lp.report.l1"), e: "−19%", tone: "down" },
      { a: SHOPS.vos, b: t("lp.report.l2"), e: "—", tone: "stock" },
      { a: SHOPS.noord, b: t("lp.report.l3"), e: "+13%", tone: "up" },
    ],
    foot: t("lp.scene.report.foot"),
  },
};
const ORDER = Object.keys(SCENES);

const stageRows = document.getElementById("stage-rows");
const stageTitle = document.getElementById("stage-title");
const stageFoot = document.getElementById("stage-foot");
const rail = document.getElementById("story-rail");

if (stageRows && rail) {
  ORDER.forEach((id, i) => {
    const li = el("li", "rail-stop");
    li.dataset.scene = id;
    li.append(el("span", "rail-n", String(i + 1)), el("span", "rail-label", t(`lp.rail.${id}`)));
    rail.append(li);
  });

  let current = null;
  const go = (id) => {
    if (id === current || !SCENES[id]) return;
    current = id;
    const scene = SCENES[id];
    stageTitle.textContent = scene.title;
    stageFoot.textContent = scene.foot || "";
    showRows(stageRows, scene.rows);
    for (const stop of rail.children) stop.toggleAttribute("data-active", stop.dataset.scene === id);
    for (const step of document.querySelectorAll(".step")) step.toggleAttribute("data-active", step.dataset.scene === id);
  };
  go(ORDER[0]);

  // A step is "on" while it crosses the middle band of the viewport.
  const io = new IntersectionObserver((entries) => {
    for (const entry of entries) if (entry.isIntersecting) go(entry.target.dataset.scene);
  }, { rootMargin: "-45% 0px -45% 0px" });
  document.querySelectorAll(".step").forEach((s) => io.observe(s));
}

/* ---------------------------------------------------------------- plans */
const plansEl = document.getElementById("plans");

function planCard(plan) {
  const cta = el("a", `btn ${plan.id === "pro" ? "btn-primary" : ""} plan-cta`.trim(),
    plan.id === "free" ? t("lp.cta") : t("plan.choose", { name: plan.name }));
  cta.href = plan.id === "free" ? "/app/#signup" : `/app/#signup?plan=${encodeURIComponent(plan.id)}`;
  return planBoard(plan, { featured: plan.id === "pro", action: cta });
}

async function loadPlans() {
  try {
    const response = await fetch("/billing/plans", { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const { plans } = await response.json();
    if (!Array.isArray(plans) || !plans.length) throw new Error("no plans returned");
    plansEl.replaceChildren(...plans.map(planCard));
    anime?.then(({ animate: a, stagger }) => a(plansEl.querySelectorAll(".lp-plan"), { opacity: [0, 1], translateY: [12, 0], duration: 420, delay: stagger(90), ease: "outExpo" }));
  } catch (error) {
    // Never leave a spinner on the pricing section: the copy still sells, the CTA still works.
    const p = el("p", "plans-error", t("lp.plans.error"));
    const a = el("a", "btn btn-primary", t("lp.cta"));
    a.href = "/app/#signup";
    plansEl.replaceChildren(p, a);
    console.warn("Could not load plans:", error);
  }
}
if (plansEl) loadPlans();
