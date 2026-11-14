// Browser fakes for node --test, installed on globalThis before any app module is imported.
// Import this file first, then load the module under test with `await import(...)`.
// The fakes stay minimal: only what i18n.js and api.js touch at import (localStorage,
// navigator.language, document.documentElement.lang) plus location and fetch for later items.

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const DEFAULT_LANGUAGE = "en-GB";
const DEFAULT_HREF = "http://localhost/";

// In-memory localStorage; the browser stores strings only, so values are coerced.
function makeStorage() {
  const map = new Map();
  return {
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => void map.set(String(key), String(value)),
    removeItem: (key) => void map.delete(key),
    clear: () => void map.clear(),
  };
}

const storage = makeStorage();
const navigatorFake = { language: DEFAULT_LANGUAGE };
// i18n.js sets document.documentElement.lang at import; nothing else touches the DOM there.
const documentFake = { documentElement: { lang: "" } };
const locationState = { hash: "", href: DEFAULT_HREF };
locationState.assign = (url) => {
  locationState.href = url;
};

let fetchHandler = null;

/** Route every non-file fetch through fn(href, init); pass null to clear. */
export function setFetch(fn) {
  fetchHandler = fn;
}

// file: URLs are served from disk so `new URL("./i18n/en.json", import.meta.url)` works as in the browser.
async function serveFile(href) {
  const text = await readFile(fileURLToPath(href), "utf8");
  return new Response(text, { status: 200, headers: { "content-type": href.endsWith(".json") ? "application/json" : "text/plain" } });
}

const fetchFake = async (input, init) => {
  const href = input instanceof URL ? input.href : String(input);
  if (href.startsWith("file:")) return serveFile(href);
  if (!fetchHandler) throw new Error(`fetch(${href}) has no handler; call setFetch(fn)`);
  return fetchHandler(href, init);
};

function install(name, value) {
  Object.defineProperty(globalThis, name, { value, writable: true, configurable: true });
}

install("localStorage", storage);
install("navigator", navigatorFake);
install("document", documentFake);
install("location", locationState);
install("fetch", fetchFake);

/** Restore every fake to its import-time state; call between tests. */
export function resetEnv() {
  storage.clear();
  navigatorFake.language = DEFAULT_LANGUAGE;
  documentFake.documentElement.lang = "";
  locationState.hash = "";
  locationState.href = DEFAULT_HREF;
  fetchHandler = null;
}
