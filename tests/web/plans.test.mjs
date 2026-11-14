// cadence() against the real crawl intervals from app/billing/plans.py.
// planBoard needs document.createElement, which env's minimal document fake does not have, so it stays untested here.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // installs localStorage/navigator/document before plans.js (via i18n.js) touches them at import
import { cadence } from "../../app/web/plans.js";

// crawl_frequency_hours as defined in app/billing/plans.py (FREE, PRO, BUSINESS).
const HOURS = { free: 168, pro: 24, business: 12 };

test("cadence maps the plan crawl intervals", () => {
  assert.equal(cadence(HOURS.free), "Checked weekly");
  assert.equal(cadence(HOURS.pro), "Checked daily");
  assert.equal(cadence(HOURS.business), "Checked twice a day");
});

test("cadence boundaries: 12 and below twice daily, up to 24 daily", () => {
  assert.equal(cadence(6), "Checked twice a day");
  assert.equal(cadence(12), "Checked twice a day");
  assert.equal(cadence(13), "Checked daily");
  assert.equal(cadence(24), "Checked daily");
  assert.equal(cadence(25), "Checked weekly");
});