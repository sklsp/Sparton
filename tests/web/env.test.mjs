// The fakes in env.mjs behave like the browser bits the app modules touch at import.
import { test } from "node:test";
import assert from "node:assert/strict";
import { setFetch, resetEnv } from "./env.mjs";

test("localStorage round trip", () => {
  localStorage.setItem("sparton.lang", "nl");
  assert.equal(localStorage.getItem("sparton.lang"), "nl");
  // The browser stores strings only.
  localStorage.setItem("n", 42);
  assert.equal(localStorage.getItem("n"), "42");
  assert.equal(localStorage.getItem("missing"), null);
  localStorage.removeItem("sparton.lang");
  assert.equal(localStorage.getItem("sparton.lang"), null);
  localStorage.clear();
  assert.equal(localStorage.getItem("n"), null);
});

test("fetch serves file: URLs from disk", async () => {
  const url = new URL("../../app/web/i18n/en.json", import.meta.url).href;
  const res = await fetch(url);
  assert.ok(res.ok);
  const data = await res.json();
  assert.equal(data["nav.overview"], "Overview");
});

test("fetch hands other URLs to the setFetch handler", async () => {
  resetEnv();
  let seen;
  setFetch((href, init) => {
    seen = [href, init];
    return new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "content-type": "application/json" } });
  });
  const res = await fetch("/auth/login", { method: "POST" });
  assert.deepEqual(seen, ["/auth/login", { method: "POST" }]);
  assert.equal((await res.json()).ok, true);
});

test("fetch without a handler throws for non-file URLs", async () => {
  resetEnv();
  await assert.rejects(() => fetch("/nope"), /setFetch/);
});

test("resetEnv restores the defaults", () => {
  localStorage.setItem("k", "v");
  navigator.language = "nl-NL";
  document.documentElement.lang = "nl";
  location.hash = "#plans";
  setFetch(() => new Response());
  resetEnv();
  assert.equal(localStorage.getItem("k"), null);
  assert.equal(navigator.language, "en-GB");
  assert.equal(document.documentElement.lang, "");
  assert.equal(location.hash, "");
});

test("i18n.js imports under the fakes and loads en.json", async () => {
  resetEnv();
  const i18n = await import("../../app/web/i18n.js");
  assert.equal(i18n.lang, "en");
  assert.equal(document.documentElement.lang, "en");
  // A real key from app/web/i18n/en.json: the dictionary really loaded.
  assert.equal(i18n.t("nav.overview"), "Overview");
});
