// Format helpers: i18n.js (locale-aware, en-IE or nl-NL) and ui.js (fixed en-US). Checked against real Intl output.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import { resetEnv } from "./env.mjs";
import * as ui from "../../app/web/ui.js";

// Two shared instances, one per language. The case numbers match i18n.test.mjs on purpose: Node merges
// coverage per URL across workers (union), so both files' profiles for ?case=1 and ?case=2 combine into a
// single high-coverage profile instead of thirteen thin ones.
resetEnv(); // no saved lang, navigator en-GB: locale is en-IE
const i18nEn = await import("../../app/web/i18n.js?case=1");
navigator.language = "nl-BE"; // no saved lang: the browser decides (Dutch)
const i18nNl = await import("../../app/web/i18n.js?case=2");

test("fmtNumber formats en-IE numbers", () => {
  assert.equal(i18nEn.fmtNumber(0), "0");
  assert.equal(i18nEn.fmtNumber(-5), "-5");
  assert.equal(i18nEn.fmtNumber(null), "0"); // Number(v ?? 0)
  assert.equal(i18nEn.fmtNumber(undefined), "0");
  assert.equal(i18nEn.fmtNumber(1234567.89), "1,234,567.89");
  assert.equal(i18nEn.fmtNumber(999999999999), "999,999,999,999");
});

test("fmtNumber passes opts through to Intl", () => {
  assert.equal(i18nEn.fmtNumber(1234.5678, { maximumFractionDigits: 1 }), "1,234.6");
});

test("fmtNumber switches to nl-NL when lang is nl", () => {
  assert.equal(i18nNl.fmtNumber(1234567.89), "1.234.567,89");
});

test("fmtMoney formats EUR by default", () => {
  assert.equal(i18nEn.fmtMoney(0), "\u20ac0.00");
  assert.equal(i18nEn.fmtMoney(-5), "-\u20ac5.00");
  assert.equal(i18nEn.fmtMoney(1234.5), "\u20ac1,234.50");
  assert.equal(i18nEn.fmtMoney(1e9), "\u20ac1,000,000,000.00");
});

test("fmtMoney honours the currency and digits arguments", () => {
  assert.equal(i18nEn.fmtMoney(5, "USD"), "US$5.00"); // en-IE spells USD as US$
  assert.equal(i18nEn.fmtMoney(5, "EUR", 0), "\u20ac5");
  assert.equal(i18nEn.fmtMoney(5, ""), "\u20ac5.00"); // an empty currency falls back to EUR
});

test("fmtMoney shows a dash for null and NaN", () => {
  assert.equal(i18nEn.fmtMoney(null), "\u2014");
  assert.equal(i18nEn.fmtMoney(undefined), "\u2014");
  assert.equal(i18nEn.fmtMoney(Number.NaN), "\u2014");
});

test("fmtMoney switches to nl-NL when lang is nl", () => {
  // Compared against the same Intl call: ICU may put a no-break space between symbol and number.
  assert.equal(
    i18nNl.fmtMoney(1234.5),
    new Intl.NumberFormat("nl-NL", { style: "currency", currency: "EUR", minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(1234.5)
  );
});

test("fmtDate uses day numeric and month short by default", () => {
  assert.equal(i18nEn.fmtDate(new Date(2026, 0, 5)), "5 Jan");
});

test("fmtDate honours custom opts", () => {
  assert.equal(i18nEn.fmtDate(new Date(2026, 0, 5), { year: "numeric", month: "long" }), "January 2026");
});

test("fmtDate throws RangeError for an invalid date", () => {
  assert.throws(() => i18nEn.fmtDate(new Date("nope")), RangeError);
});

test("fmtDate switches to nl-NL when lang is nl", () => {
  assert.equal(i18nNl.fmtDate(new Date(2026, 0, 5)), "5 jan");
});

test("fmtRelative collapses plus/minus one day with numeric auto", () => {
  assert.equal(i18nEn.fmtRelative(-1, "day"), "yesterday");
  assert.equal(i18nEn.fmtRelative(1, "day"), "tomorrow");
});

test("fmtRelative formats other values", () => {
  assert.equal(i18nEn.fmtRelative(-3, "hours"), "3 hours ago");
  assert.equal(i18nEn.fmtRelative(0, "minutes"), "this minute");
});

test("ui.num formats en-US numbers", () => {
  assert.equal(ui.num(0), "0");
  assert.equal(ui.num(-5), "-5");
  assert.equal(ui.num(null), "0");
  assert.equal(ui.num(undefined), "0");
  assert.equal(ui.num(1234567.891), "1,234,567.891");
});

test("ui.money formats whole US dollars", () => {
  assert.equal(ui.money(0), "$0");
  assert.equal(ui.money(-5), "-$5");
  assert.equal(ui.money(null), "$0");
  assert.equal(ui.money(1234.6), "$1,235");
  assert.equal(ui.money(1e9), "$1,000,000,000");
});

test("ui.bytes scales through B KB MB GB", () => {
  assert.equal(ui.bytes(0), "0 B");
  assert.equal(ui.bytes(null), "0 B");
  assert.equal(ui.bytes(undefined), "0 B");
  assert.equal(ui.bytes(512), "512 B");
  assert.equal(ui.bytes(1023), "1023 B");
  assert.equal(ui.bytes(1024), "1.0 KB");
  assert.equal(ui.bytes(1536), "1.5 KB");
  assert.equal(ui.bytes(1048576), "1.0 MB");
  assert.equal(ui.bytes(5 * 1024 ** 3), "5.0 GB");
});

test("ui.bytes clamps negative input to 0 B and keeps fractions in bytes", () => {
  assert.equal(ui.bytes(-5), "0 B"); // the !(n > 0) guard catches negatives as well
  assert.equal(ui.bytes(0.5), "1 B");
});

test("ui.ago buckets recent timestamps", () => {
  const now = Date.now();
  assert.equal(ui.ago(new Date(now - 30 * 1000).toISOString()), "just now");
  assert.equal(ui.ago(new Date(now - 2 * 60 * 1000).toISOString()), "2m ago");
  assert.equal(ui.ago(new Date(now - 90 * 60 * 1000).toISOString()), "2h ago");
  assert.equal(ui.ago(new Date(now - 2 * 86400 * 1000).toISOString()), "2d ago");
  assert.equal(ui.ago(new Date(now - 5 * 86400 * 1000).toISOString()), "5d ago");
});

test("ui.ago shows a plain date beyond 30 days", () => {
  // Locale-dependent, so compared against the same toLocaleDateString call.
  assert.equal(ui.ago("2020-01-01 00:00:00"), new Date("2020-01-01T00:00:00Z").toLocaleDateString());
});

test("ui.when renders naive datetimes as UTC", () => {
  // The API sends str(datetime) without a zone; the first space becomes T and Z is appended.
  assert.equal(ui.when("2026-01-05 10:30:00"), new Date("2026-01-05T10:30:00Z").toLocaleString());
});

test("ui.when keeps an explicit UTC offset", () => {
  assert.equal(ui.when("2026-01-05 10:30:00+02:00"), new Date("2026-01-05T10:30:00+02:00").toLocaleString());
});

test("ui.ago and ui.when show a dash for null or unparseable input", () => {
  assert.equal(ui.ago(null), "\u2014");
  assert.equal(ui.ago(undefined), "\u2014");
  assert.equal(ui.ago(""), "\u2014");
  assert.equal(ui.ago("not a date"), "\u2014");
  assert.equal(ui.when(null), "\u2014");
  assert.equal(ui.when("nope"), "\u2014");
});

test("ui.tone maps statuses to semantic colours", () => {
  assert.equal(ui.tone("completed"), "success");
  assert.equal(ui.tone("SUCCEEDED"), "success");
  assert.equal(ui.tone("failed"), "danger");
  assert.equal(ui.tone("REJECTED"), "danger");
  assert.equal(ui.tone("pending"), "warning");
  assert.equal(ui.tone("AWAITING_APPROVAL"), "warning");
  assert.equal(ui.tone("running"), "info");
  assert.equal(ui.tone("IN_PROGRESS"), "info");
  assert.equal(ui.tone("unknown-status"), "neutral");
  assert.equal(ui.tone(null), "neutral");
  assert.equal(ui.tone(""), "neutral");
});

test("ui.title prettifies identifiers", () => {
  assert.equal(ui.title("price_cut"), "Price Cut");
  assert.equal(ui.title("in-progress"), "In Progress");
  assert.equal(ui.title("a_b-c_d"), "A B C D");
  assert.equal(ui.title(null), "");
  assert.equal(ui.title(42), "42");
});