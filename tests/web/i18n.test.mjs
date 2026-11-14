// The two dictionaries stay in lockstep, and t() behaves as documented. Checked against the real files.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { resetEnv } from "./env.mjs";

const en = JSON.parse(await readFile(new URL("../../app/web/i18n/en.json", import.meta.url), "utf8"));
const nl = JSON.parse(await readFile(new URL("../../app/web/i18n/nl.json", import.meta.url), "utf8"));

function placeholders(value) {
  return [...new Set([...value.matchAll(/\{(\w+)\}/g)].map((m) => m[1]))].sort();
}

test("en and nl have exactly the same keys", () => {
  const missingInNl = Object.keys(en).filter((k) => !(k in nl));
  const missingInEn = Object.keys(nl).filter((k) => !(k in en));
  assert.deepEqual(missingInNl, [], `keys missing from nl.json: ${missingInNl.join(", ")}`);
  assert.deepEqual(missingInEn, [], `keys missing from en.json: ${missingInEn.join(", ")}`);
});

test("every key has the same {placeholder} set in both languages", () => {
  const mismatches = [];
  for (const key of Object.keys(en)) {
    if (!(key in nl)) continue; // covered by the key parity test above
    const a = placeholders(en[key]);
    const b = placeholders(nl[key]);
    if (a.join(",") !== b.join(",")) mismatches.push(`${key}: en={${a}} nl={${b}}`);
  }
  assert.deepEqual(mismatches, [], `placeholder sets differ: ${mismatches.join("; ")}`);
});

test("no value is empty", () => {
  const empties = [];
  for (const [name, dict] of [["en.json", en], ["nl.json", nl]]) {
    for (const [key, value] of Object.entries(dict)) {
      if (typeof value !== "string" || value.length === 0) empties.push(`${name}:${key}`);
    }
  }
  assert.deepEqual(empties, [], `empty values: ${empties.join(", ")}`);
});

// A fresh module instance per case: the query string busts the ESM cache.
let caseNo = 0;
async function freshI18n() {
  return await import(`../../app/web/i18n.js?case=${++caseNo}`);
}

test("t() translates a known key", async () => {
  resetEnv(); // no saved lang, navigator en-GB: the English dictionary loads
  const i18n = await freshI18n();
  assert.equal(i18n.lang, "en");
  assert.equal(i18n.t("nav.overview"), "Overview");
});

test("t() returns the key itself for a missing key", async () => {
  resetEnv();
  const i18n = await freshI18n();
  assert.equal(i18n.t("no.such.key"), "no.such.key");
});

test("t() interpolates {name}", async () => {
  resetEnv();
  const i18n = await freshI18n();
  assert.equal(i18n.t("plan.choose", { name: "Pro" }), "Choose Pro");
});

test("t() leaves an unknown placeholder as written", async () => {
  resetEnv();
  const i18n = await freshI18n();
  // plan.choose has {name}; a different var does not touch it.
  assert.equal(i18n.t("plan.choose", { other: "x" }), "Choose {name}");
});

test("t() uses the .one plural form for n=1 only", async () => {
  resetEnv();
  const i18n = await freshI18n();
  assert.equal(i18n.t("plan.shops", { n: 1 }), "1 shop");
  assert.equal(i18n.t("plan.shops", { n: 3 }), "3 shops");
});

test("lang follows localStorage first", async () => {
  resetEnv();
  localStorage.setItem("sparton.lang", "nl");
  navigator.language = "en-IE"; // the saved choice wins over the browser language
  const i18n = await freshI18n();
  assert.equal(i18n.lang, "nl");
  assert.equal(document.documentElement.lang, "nl");
  // The Dutch dictionary really loaded.
  assert.equal(i18n.t("nav.overview"), nl["nav.overview"]);
});

test("lang ignores an unknown saved value", async () => {
  resetEnv();
  localStorage.setItem("sparton.lang", "fr");
  navigator.language = "nl-NL";
  const i18n = await freshI18n();
  assert.equal(i18n.lang, "nl");
});

test("lang falls back to navigator.language: nl* gives nl", async () => {
  resetEnv();
  navigator.language = "nl-BE";
  const i18n = await freshI18n();
  assert.equal(i18n.lang, "nl");
});

test("lang falls back to navigator.language: anything else en", async () => {
  resetEnv();
  navigator.language = "fr-FR";
  const i18n = await freshI18n();
  assert.equal(i18n.lang, "en");
});
