// The API client: auth header, JSON bodies, ApiError mapping, 401 callbacks, network errors, settleAll.
import { test } from "node:test";
import assert from "node:assert/strict";
import { resetEnv, setFetch } from "./env.mjs";
import * as api from "../../app/web/api.js";

const json = { "content-type": "application/json" };

// Record every (href, init) pair the client hands to fetch.
function capture(handler) {
  const calls = [];
  setFetch((href, init) => {
    calls.push({ href, init });
    return handler(href, init);
  });
  return calls;
}

test("token.set/get/clear round trip through localStorage", () => {
  resetEnv();
  assert.equal(api.token.get(), null);
  api.token.set("abc");
  assert.equal(localStorage.getItem("sparton.token"), "abc");
  assert.equal(api.token.get(), "abc");
  api.token.clear();
  assert.equal(api.token.get(), null);
});

test("the bearer token from localStorage is sent", async () => {
  resetEnv();
  api.token.set("tok-123");
  const calls = capture(() => new Response(JSON.stringify({ ok: true }), { headers: json }));
  await api.api.health();
  assert.equal(calls[0].init.headers.Authorization, "Bearer tok-123");
});

test("no Authorization header when there is no token", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ ok: true }), { headers: json }));
  await api.api.health();
  assert.equal(calls[0].init.headers.Authorization, undefined);
});

test("post sends the JSON body with a content type", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ id: 1 }), { headers: json }));
  await api.api.createShop({ name: "Acme" });
  assert.equal(calls[0].init.method, "POST");
  assert.equal(calls[0].init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0].init.body), { name: "Acme" });
});

test("patch and put send the JSON body with a content type", async () => {
  resetEnv();
  const calls = capture(() => new Response(null, { status: 204 }));
  await api.api.updateShop(7, { name: "New" });
  assert.equal(calls[0].init.method, "PATCH");
  assert.deepEqual(JSON.parse(calls[0].init.body), { name: "New" });
  await api.put("/x", { a: 1 });
  assert.equal(calls[1].init.method, "PUT");
  assert.equal(calls[1].init.headers["Content-Type"], "application/json");
});

test("get sends no body and no content type", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify([]), { headers: json }));
  await api.api.shops();
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].init.body, undefined);
  assert.equal(calls[0].init.headers["Content-Type"], undefined);
});

test("changes builds its query string", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify([]), { headers: json }));
  await api.api.changes({ shopId: 4, kind: "price_cut", unacknowledgedOnly: true });
  assert.equal(calls[0].href, "/changes?days=30&limit=50&shop_id=4&kind=price_cut&unacknowledged_only=true");
});

test("a non-2xx response throws ApiError with the server's string detail", async () => {
  resetEnv();
  setFetch(() => new Response(JSON.stringify({ detail: "Shop not found" }), { status: 404, headers: json }));
  await assert.rejects(api.api.shop(9), (err) =>
    err instanceof api.ApiError && err.status === 404 && err.detail === "Shop not found" && err.message === "Shop not found");
});

test("validation errors join the FastAPI detail array", async () => {
  resetEnv();
  const detail = [{ msg: "name is required" }, { msg: "price must be positive" }];
  setFetch(() => new Response(JSON.stringify({ detail }), { status: 422, headers: json }));
  await assert.rejects(api.api.createShop({}), (err) =>
    err.status === 422 && err.message === "name is required, price must be positive");
});

test("plan limit errors use detail.message", async () => {
  resetEnv();
  const detail = { message: "Monthly crawl limit reached", limit: 10, plan: "Starter" };
  setFetch(() => new Response(JSON.stringify({ detail }), { status: 403, headers: json }));
  await assert.rejects(api.api.crawlShop(1), (err) =>
    err.status === 403 && err.message === "Monthly crawl limit reached");
});

test("a non-JSON error body keeps the generic message", async () => {
  resetEnv();
  setFetch(() => new Response("<html>boom</html>", { status: 502, headers: { "content-type": "text/html" } }));
  await assert.rejects(api.api.health(), (err) =>
    err.status === 502 && err.message === "Request failed (502)" && err.detail === undefined);
});

test("a 401 clears the token, fires every callback and throws", async () => {
  resetEnv();
  api.token.set("stale");
  let a = 0;
  let b = 0;
  const cbA = () => a++;
  const cbB = () => b++;
  api.onUnauthorized.add(cbA);
  api.onUnauthorized.add(cbB);
  try {
    setFetch(() => new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401, headers: json }));
    await assert.rejects(api.api.me(), (err) =>
      err instanceof api.ApiError && err.status === 401 && err.message === "Your session expired. Sign in again.");
    assert.equal(a, 1);
    assert.equal(b, 1);
    assert.equal(api.token.get(), null);
  } finally {
    api.onUnauthorized.delete(cbA);
    api.onUnauthorized.delete(cbB);
  }
});

test("a network error becomes an ApiError with status 0", async () => {
  resetEnv();
  setFetch(() => {
    throw new TypeError("fetch failed");
  });
  await assert.rejects(api.api.health(), (err) =>
    err instanceof api.ApiError && err.status === 0 && /server running/.test(err.message));
});

test("an AbortError is rethrown as-is", async () => {
  resetEnv();
  const abort = new Error("Aborted");
  abort.name = "AbortError";
  setFetch(() => {
    throw abort;
  });
  await assert.rejects(api.api.health(), (err) => err === abort);
});

test("a 204 response resolves to null", async () => {
  resetEnv();
  setFetch(() => new Response(null, { status: 204 }));
  assert.equal(await api.api.deleteShop(3), null);
});

test("JSON responses are parsed and text responses stay strings", async () => {
  resetEnv();
  const calls = capture((href) =>
    href.endsWith("/metrics")
      ? new Response("# metric\nup 1", { headers: { "content-type": "text/plain" } })
      : new Response(JSON.stringify({ ok: true }), { headers: json }));
  assert.deepEqual(await api.api.health(), { ok: true });
  assert.equal(calls[0].init.method, "GET");
  assert.equal(await api.api.metrics(), "# metric\nup 1");
});

test("settleAll rethrows when every source failed", async () => {
  const boom = new api.ApiError("down", 0);
  await assert.rejects(api.settleAll([Promise.reject(boom), Promise.reject(new Error("x"))]), (err) => err === boom);
});

test("settleAll degrades gracefully on partial failure", async () => {
  const results = await api.settleAll([Promise.resolve(1), Promise.reject(new Error("nope")), Promise.resolve(3)]);
  assert.equal(results.length, 3);
  assert.deepEqual(api.settledValue(results[0], null), 1);
  assert.deepEqual(api.settledValue(results[1], "fallback"), "fallback");
});

test("settleAll returns an empty list for no sources", async () => {
  assert.deepEqual(await api.settleAll([]), []);
});

test("settledValue picks the value or the fallback", () => {
  assert.equal(api.settledValue({ status: "fulfilled", value: 42 }, null), 42);
  assert.equal(api.settledValue({ status: "rejected", reason: new Error() }, "fb"), "fb");
});