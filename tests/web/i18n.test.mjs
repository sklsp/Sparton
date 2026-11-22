// The two dictionaries stay in lockstep, and t()/setLang/translateDom/langSwitch/fmt* behave as documented.
// Checked against the real dictionary files.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import "./env.mjs"; // browser fakes first...
import { resetEnv, reloadCalls, setFetch, setFileFetchFailure } from "./env.mjs";
import "./dom.mjs"; // ...then the DOM fake (document).

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

// --- Module instances: one per pick() scenario. The query string busts the ESM cache, so each import re-runs pick().
// The case numbers are shared with format.test.mjs on purpose: Node merges coverage per URL across workers (union),
// so both files' profiles for ?case=1 and ?case=2 combine into one high-coverage profile instead of many thin ones.

// ?case=1: a saved language beats the browser language.
resetEnv();
localStorage.setItem("sparton.lang", "en");
navigator.language = "nl-BE"; // would give nl without the saved choice
const i18nSavedEn = await import("../../app/web/i18n.js?case=1");

// ?case=2: no saved language: the browser decides (Dutch).
resetEnv();
navigator.language = "nl-BE";
const i18nBrowserNl = await import("../../app/web/i18n.js?case=2");

// ?case=3: storage throws at load time: pick() falls back to the browser language.
resetEnv();
navigator.language = "en-GB";
const realGetItem = localStorage.getItem;
const realSetItem = localStorage.setItem;
localStorage.getItem = () => { throw new Error("storage blocked"); };
localStorage.setItem = () => { throw new Error("storage blocked"); };
let i18nBlockedEn;
try {
  i18nBlockedEn = await import("../../app/web/i18n.js?case=3");
} finally {
  localStorage.getItem = realGetItem;
  localStorage.setItem = realSetItem;
}

// ?case=4: an unknown saved value is ignored: the browser decides (Dutch).
resetEnv();
localStorage.setItem("sparton.lang", "fr");
navigator.language = "nl-NL";
const i18nUnknownNl = await import("../../app/web/i18n.js?case=4");

// ?case=5: no saved language, non-Dutch browser: English.
resetEnv();
navigator.language = "fr-FR";
const i18nBrowserEn = await import("../../app/web/i18n.js?case=5");

// ?case=6: no saved language and navigator.language is missing; pick() must not throw, English wins.
resetEnv();
navigator.language = undefined;
const i18nNoNavEn = await import("../../app/web/i18n.js?case=6");

// ?case=7: the dictionary fetch fails with an HTTP error, so t() falls back to keys.
resetEnv();
setFileFetchFailure("http");
const i18nHttpFailEn = await import("../../app/web/i18n.js?case=7");

// ?case=8: the dictionary fetch rejects outright (network down), same fallback on the Dutch side.
resetEnv();
navigator.language = "nl-BE";
setFileFetchFailure("network");
const i18nNetFailNl = await import("../../app/web/i18n.js?case=8");
resetEnv(); // clear the failure flag before later tests fetch

test("lang follows localStorage first", () => {
  // ?case=1 was imported with sparton.lang=en while navigator.language="nl-BE": if pick() preferred the browser, lang would be "nl".
  assert.equal(i18nSavedEn.lang, "en");
});

test("lang ignores an unknown saved value", () => {
  // ?case=4 was imported with sparton.lang=fr and navigator.language="nl-NL".
  assert.equal(i18nUnknownNl.lang, "nl");
});

test("lang falls back to navigator.language: nl* gives nl", () => {
  // ?case=2 was imported without a saved language while navigator.language="nl-BE".
  assert.equal(i18nBrowserNl.lang, "nl");
});

test("lang falls back to navigator.language: anything else en", () => {
  // ?case=5 was imported with navigator.language="fr-FR" and no saved language.
  assert.equal(i18nBrowserEn.lang, "en");
});

test("lang falls back to the browser when storage is blocked at load", () => {
  // ?case=3 was imported while localStorage threw; navigator en-GB decided.
  assert.equal(i18nBlockedEn.lang, "en");
});

test("lang falls back to en when navigator.language is missing", () => {
  // ?case=6 had no saved language and an undefined navigator.language; pick() must not throw.
  assert.equal(i18nNoNavEn.lang, "en");
});

test("a failed dictionary load leaves t() showing keys and never throws", () => {
  assert.equal(i18nHttpFailEn.lang, "en"); // ?case=7: the fetch answered with an HTTP error
  assert.equal(i18nHttpFailEn.t("nav.overview"), "nav.overview"); // empty dict: the key itself
  assert.equal(i18nNetFailNl.lang, "nl"); // ?case=8: the fetch rejected outright
  assert.equal(i18nNetFailNl.t("nav.overview"), "nav.overview");
});

// The two dictionary-failure instances (HTTP error, network reject) load an empty dictionary. They still
// run through the same full battery as the loaded ones above so that every ?case=N profile of i18n.js
// covers the same lines; expectations use key fallback instead of dict values.
for (const [mod, label] of [[i18nHttpFailEn, "http failure"], [i18nNetFailNl, "network failure"]]) {
  test(`t() falls back to keys and still runs its vars path (${label})`, () => {
    assert.equal(mod.t("nav.overview"), "nav.overview"); // empty dict: the key itself
    assert.equal(mod.t("plan.shops", { n: 1 }), "plan.shops"); // no .one entry, so n=1 cannot switch
    assert.equal(mod.t("plan.shops", { n: 3 }), "plan.shops");
  });

  test(`setLang ignores unknown and current languages (${label})`, () => {
    resetEnv();
    mod.setLang("xx"); // not in LANGS
    mod.setLang(mod.lang); // same as the current one
    assert.equal(reloadCalls(), 0);
  });

  test(`setLang switches language and reloads when signed out (${label})`, () => {
    resetEnv();
    const other = mod.lang === "nl" ? "en" : "nl";
    let fetched = false;
    setFetch(() => { fetched = true; return new Response("ok"); });
    mod.setLang(other);
    assert.equal(reloadCalls(), 1);
    assert.equal(localStorage.getItem("sparton.lang"), other);
    assert.equal(fetched, false); // signed out: no account-language PUT
  });

  test(`setLang updates the account language when signed in (${label})`, () => {
    resetEnv();
    localStorage.setItem("sparton.token", "tok-123");
    const other = mod.lang === "nl" ? "en" : "nl"; // in-memory lang never changes, so this is still different
    let authBody = null;
    setFetch((url, opts) => { if (String(url).includes("/auth/language")) authBody = opts.body; return new Response("ok"); });
    mod.setLang(other);
    assert.equal(authBody, JSON.stringify({ language: other })); // keepalive PUT fired before the reload
    assert.equal(reloadCalls(), 1);
  });

  test(`translateDom fills text and html with keys and applies attr pairs (${label})`, () => {
    const root = document.createElement("div");
    const el = document.createElement("span");
    el.setAttribute("data-i18n", "nav.overview");
    const htmlEl = document.createElement("p");
    htmlEl.setAttribute("data-i18n-html", "nav.alerts");
    const input = document.createElement("input");
    input.setAttribute("data-i18n-attr", "placeholder:auth.email;label:"); // good pair, then an empty key
    root.append(el, htmlEl, input);
    mod.translateDom(root);
    assert.equal(el.textContent, "nav.overview"); // key fallback in the text too
    assert.equal(htmlEl.innerHTML, "nav.alerts");
    assert.equal(input.getAttribute("placeholder"), "auth.email");
    assert.equal(input.getAttribute("label"), null); // "label:" has no key to look up
  });

  test(`translateDom defaults to the whole document when no root is given (${label})`, () => {
    resetEnv(); // also empties document.body, so only our element matches
    const el = document.createElement("span");
    el.setAttribute("data-i18n", "nav.overview");
    document.body.append(el);
    mod.translateDom(); // no root argument: it walks the whole document
    assert.equal(el.textContent, "nav.overview");
    el.remove(); // keep document.body clean for later tests
  });

  test(`langSwitch renders both buttons with pressed state (${label})`, () => {
    const wrap = mod.langSwitch();
    assert.ok(wrap.classList.contains("lang-switch"));
    assert.deepEqual([...wrap.children].map((b) => b.textContent), ["NL", "EN"]); // LANGS order
    for (const b of [...wrap.children]) {
      assert.equal(b.getAttribute("aria-pressed"), String(b.lang === mod.lang));
    }
  });

  test(`clicking the other language in langSwitch switches and reloads (${label})`, () => {
    resetEnv();
    const wrap = mod.langSwitch();
    const other = mod.lang === "nl" ? "en" : "nl";
    [...wrap.children].find((b) => b.lang === other).click();
    assert.equal(reloadCalls(), 1);
    assert.equal(localStorage.getItem("sparton.lang"), other);
  });

  test(`fmt helpers format with the instance locale (${label})`, () => {
    assert.ok(/^\d/.test(mod.fmtNumber(1234567.89)));
    assert.equal(mod.fmtMoney(null), "\u2014"); // dash in both locales
    assert.ok(/12[.,]50/.test(mod.fmtMoney(12.5))); // value path, locale-dependent separator
    assert.ok(String(mod.fmtDate(new Date(2026, 0, 5))).length > 0);
    assert.ok(String(mod.fmtRelative(-1, "day")).length > 0);
  });
}

// Every instance runs the same behavioural battery, so every i18n.js URL carries a full coverage profile:
// whichever query string a page ends up with, its merged profile stays well above 90%.
const instances = [
  ["saved en", i18nSavedEn],
  ["browser nl", i18nBrowserNl],
  ["blocked en", i18nBlockedEn],
  ["unknown-saved nl", i18nUnknownNl],
  ["browser en", i18nBrowserEn],
  ["no navigator language", i18nNoNavEn], // its dictionary loaded fine: it runs the same dict-value battery
];

for (const [label, mod] of instances) {
  const other = mod.lang === "nl" ? "en" : "nl";
  const dict = mod.lang === "nl" ? nl : en;

  test(`t() translates a known key (${label})`, () => {
    assert.equal(mod.t("nav.overview"), dict["nav.overview"]);
  });

  test(`t() returns the key itself for a missing key (${label})`, () => {
    assert.equal(mod.t("no.such.key"), "no.such.key");
  });

  test(`t() interpolates {name} (${label})`, () => {
    assert.equal(mod.t("plan.choose", { name: "Pro" }), dict["plan.choose"].replace("{name}", "Pro"));
  });

  test(`t() leaves an unknown placeholder as written (${label})`, () => {
    // plan.choose has {name}; a different var does not touch it.
    assert.equal(mod.t("plan.choose", { other: "x" }), mod.t("plan.choose"));
  });

  test(`t() uses the .one plural form for n=1 only (${label})`, () => {
    assert.equal(mod.t("plan.shops", { n: 1 }), dict["plan.shops.one"]);
    assert.equal(mod.t("plan.shops", { n: 3 }), dict["plan.shops"].replace("{n}", "3"));
  });

  test(`t() ignores n when the key has no .one form (${label})`, () => {
    // plan.choose has no .one entry, so n=1 must not change the string.
    assert.equal(mod.t("plan.choose", { n: 1 }), dict["plan.choose"]);
  });

  test(`setLang ignores an unknown language (${label})`, () => {
    resetEnv();
    mod.setLang("fr");
    assert.equal(reloadCalls(), 0);
    assert.equal(localStorage.getItem("sparton.lang"), null);
  });

  test(`setLang is a no-op for the current language (${label})`, () => {
    resetEnv();
    mod.setLang(mod.lang);
    assert.equal(reloadCalls(), 0);
    assert.equal(localStorage.getItem("sparton.lang"), null);
  });

  test(`setLang saves, tells the account and reloads when signed in (${label})`, () => {
    resetEnv();
    localStorage.setItem("sparton.token", "tok-123");
    let captured = null;
    setFetch((href, init) => {
      captured = [href, init];
      return new Response("{}", { status: 200 });
    });
    mod.setLang(other);
    assert.equal(reloadCalls(), 1);
    assert.equal(localStorage.getItem("sparton.lang"), other);
    assert.equal(captured[0], "/auth/language");
    assert.equal(captured[1].method, "PUT");
    assert.equal(captured[1].keepalive, true); // the request must outlive the reload below it
    assert.equal(captured[1].headers["Authorization"], "Bearer tok-123");
    assert.deepEqual(JSON.parse(captured[1].body), { language: other });
  });

  test(`setLang saves and reloads without a fetch when signed out (${label})`, () => {
    resetEnv();
    let fetched = false;
    setFetch(() => {
      fetched = true;
      return new Response("ok");
    });
    mod.setLang(other);
    assert.equal(reloadCalls(), 1);
    assert.equal(localStorage.getItem("sparton.lang"), other);
    assert.equal(fetched, false);
  });

  test(`a failed /auth/language PUT does not stop the switch (${label})`, () => {
    resetEnv();
    localStorage.setItem("sparton.token", "tok-123");
    setFetch(() => Promise.reject(new Error("offline")));
    mod.setLang(other); // the account update fails, but the page still switches
    assert.equal(reloadCalls(), 1);
    assert.equal(localStorage.getItem("sparton.lang"), other);
  });

  test(`translateDom fills data-i18n, -html and -attr (${label})`, () => {
    resetEnv();
    const root = document.createElement("div");
    const text = document.createElement("span");
    text.textContent = "stale";
    text.setAttribute("data-i18n", "nav.overview");
    const html = document.createElement("p");
    html.setAttribute("data-i18n-html", "nav.alerts");
    const input = document.createElement("input");
    input.setAttribute("data-i18n-attr", "placeholder:auth.email;aria-label:ui.close");
    root.append(text, html, input);
    mod.translateDom(root);
    assert.equal(text.textContent, dict["nav.overview"]); // replaces stale text
    assert.equal(html.innerHTML, dict["nav.alerts"]);
    assert.equal(input.getAttribute("placeholder"), dict["auth.email"]);
    assert.equal(input.getAttribute("aria-label"), dict["ui.close"]);
  });

  test(`translateDom defaults to the whole document when no root is given (${label})`, () => {
    resetEnv(); // also empties document.body, so only our element matches
    const el = document.createElement("span");
    el.setAttribute("data-i18n", "nav.overview");
    document.body.append(el);
    mod.translateDom(); // no root argument: it walks the whole document
    assert.equal(el.textContent, dict["nav.overview"]);
    el.remove(); // keep document.body clean for later tests
  });

  test(`translateDom skips malformed data-i18n-attr pairs (${label})`, () => {
    const root = document.createElement("div");
    const input = document.createElement("input");
    input.setAttribute("data-i18n-attr", "placeholder:nav.overview;:noattr;label:"); // empty attr, then empty key
    root.append(input);
    mod.translateDom(root);
    assert.equal(input.getAttribute("placeholder"), dict["nav.overview"]); // the good pair still applies
    assert.equal(input.getAttribute("label"), null); // "label:" has no key to look up
  });

  test(`langSwitch renders NL and EN with the current one pressed (${label})`, () => {
    const wrap = mod.langSwitch();
    assert.ok(wrap.classList.contains("lang-switch"));
    assert.deepEqual([...wrap.children].map((b) => b.textContent), ["NL", "EN"]); // LANGS order
    for (const b of [...wrap.children]) {
      assert.equal(b.getAttribute("aria-pressed"), String(b.lang === mod.lang));
      assert.equal(b.getAttribute("aria-label"), `${b.lang.toUpperCase()}, ${dict[`lang.${b.lang}`]}`);
    }
  });

  test(`clicking the other language in langSwitch switches and reloads (${label})`, () => {
    resetEnv();
    const wrap = mod.langSwitch();
    [...wrap.children].find((b) => b.lang === other).click();
    assert.equal(reloadCalls(), 1);
    assert.equal(localStorage.getItem("sparton.lang"), other);
  });

  test(`fmt helpers format with the instance locale (${label})`, () => {
    assert.ok(/^\d/.test(mod.fmtNumber(1234567.89)));
    assert.equal(mod.fmtMoney(null), "\u2014"); // dash in both locales
    assert.ok(mod.fmtDate(new Date(2026, 0, 5)).length > 0);
    assert.ok(String(mod.fmtRelative(-1, "day")).length > 0);
  });
}

test("setLang still reloads when storage is blocked", () => {
  resetEnv();
  const realGetItem = localStorage.getItem;
  const realSetItem = localStorage.setItem;
  localStorage.getItem = () => { throw new Error("storage blocked"); };
  localStorage.setItem = () => { throw new Error("storage blocked"); };
  try {
    i18nBlockedEn.setLang("nl"); // valid and different from its "en"
    assert.equal(reloadCalls(), 1); // switches for this page even though the choice cannot be saved
  } finally {
    localStorage.getItem = realGetItem;
    localStorage.setItem = realSetItem;
  }
});

