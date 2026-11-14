// planBoard from plans.js on the DOM fake, with the real numbers of the Pro and Free plans
// from app/billing/plans.py: the price, the limits, the featured/current states, the action.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then the DOM fake (document).
import * as plans from "../../app/web/plans.js";
import { t } from "../../app/web/i18n.js";

// The Pro plan, straight from app/billing/plans.py.
const pro = {
  id: "pro", name: "Pro", price_cents: 2900, shops: 3, competitors: 15,
  daily_tokens: 20000, crawl_frequency_hours: 24, report_history_months: 12,
  features: { weekly_report: true, price_alerts: true, assortment_alerts: true, email_digest: true, api_access: false, priority_support: false },
};

// The Free plan, straight from app/billing/plans.py.
const free = {
  id: "free", name: "Free", price_cents: 0, shops: 1, competitors: 3,
  daily_tokens: 2000, crawl_frequency_hours: 168, report_history_months: 1,
  features: { weekly_report: true, price_alerts: true, assortment_alerts: true, email_digest: false, api_access: false, priority_support: false },
};

const tiles = (el) => [...el.children].map((x) => x.textContent);

test("planBoard shows the name and the price as flaps with a real aria-label", () => {
  const card = plans.planBoard(pro);
  assert.equal(card.tagName, "ARTICLE");
  assert.ok(card.classList.contains("lp-plan"));
  assert.ok(card.classList.contains("plan-board"));
  assert.equal(card.querySelector(".plan-name").textContent, "Pro");
  const flaps = card.querySelector(".plan-flaps");
  assert.deepEqual(tiles(flaps), ["€", "2", "9"]); // no spacer tile between € and the figure
  assert.equal(flaps.getAttribute("aria-label"), "€29");
  assert.equal(card.querySelector(".plan-per").textContent, t("plan.perMonth"));
});

test("planBoard shows the limits as flap rows with spoken labels", () => {
  const card = plans.planBoard(pro);
  const [shopsRow, rivalsRow, checksRow] = [...card.querySelectorAll(".plan-row")];
  assert.equal(shopsRow.querySelector("dt").textContent, t("plan.row.shops"));
  const shopsFlaps = shopsRow.querySelector(".flaps");
  assert.deepEqual(tiles(shopsFlaps), ["3"]);
  assert.equal(shopsFlaps.getAttribute("aria-label"), t("plan.shops", { n: 3 }));

  const rivalsFlaps = rivalsRow.querySelector(".flaps");
  assert.deepEqual(tiles(rivalsFlaps), ["1", "5"]);
  assert.equal(rivalsFlaps.getAttribute("aria-label"), t("plan.competitors", { n: 15 }));

  // A 24h crawl cadence reads as daily, on the tiles and in the spoken label.
  const checksFlaps = checksRow.querySelector(".flaps");
  assert.equal(tiles(checksFlaps).join(""), t("plan.checks.daily"));
  assert.equal(checksFlaps.getAttribute("aria-label"), t("plan.freq.daily"));
});

test("planBoard lists the extras in chalk and skips features the plan lacks", () => {
  const card = plans.planBoard(pro); // Pro: no api access, no priority support
  const items = [...card.querySelector(".plan-features").children].map((li) => li.textContent);
  assert.deepEqual(items, [t("plan.history", { n: 12 }), t("plan.weeklyReport")]);
});

test("planBoard marks featured and current plans on the card itself", () => {
  const featured = plans.planBoard(pro, { featured: true });
  assert.equal(featured.getAttribute("data-featured"), "");
  assert.equal(featured.querySelector(".plan-flag").textContent, t("plan.mostShops"));

  const current = plans.planBoard(pro, { current: true });
  assert.equal(current.getAttribute("data-current"), "");
  assert.equal(current.querySelector(".plan-flag"), null); // the flag belongs to featured only

  const plain = plans.planBoard(pro);
  assert.equal(plain.getAttribute("data-featured"), null);
  assert.equal(plain.getAttribute("data-current"), null);
});

test("planBoard appends the action element under the board", () => {
  const btn = document.createElement("button");
  btn.textContent = "Upgrade";
  const card = plans.planBoard(pro, { action: btn });
  assert.equal(card.lastChild, btn);

  const none = plans.planBoard(pro); // no action: the features list stays last
  assert.ok(none.lastChild.classList.contains("plan-features"));
});

test("the free plan reads as forever and weekly", () => {
  const card = plans.planBoard(free);
  const flaps = card.querySelector(".plan-flaps");
  assert.deepEqual(tiles(flaps), ["€", "0"]);
  assert.equal(card.querySelector(".plan-per").textContent, t("plan.forever")); // price_cents is 0
  const checksFlaps = [...card.querySelectorAll(".plan-row")][2].querySelector(".flaps");
  assert.equal(checksFlaps.getAttribute("aria-label"), t("plan.freq.weekly")); // 168h cadence
});
