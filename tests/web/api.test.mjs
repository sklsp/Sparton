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

// --- every api.js method checked against its FastAPI route ------------------
// Each call is recorded by capture() and asserted on URL, HTTP method and body;
// the response shapes mirror the top-level keys of the handler in app/api/.

test("auth methods hit their routes with the right bodies", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ token: "t-1", user: { id: 1 } }), { headers: json }));
  await api.api.login("j@acme.test", "pw");
  assert.equal(calls[0].href, "/auth/login");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].init.body), { email: "j@acme.test", password: "pw" });

  await api.api.register("j@acme.test", "pw", "Acme", "en-GB");
  assert.equal(calls[1].href, "/auth/register");
  assert.deepEqual(JSON.parse(calls[1].init.body), { email: "j@acme.test", password: "pw", organization_name: "Acme", language: "en-GB" });

  await api.api.logout();
  assert.equal(calls[2].href, "/auth/logout");
  assert.equal(calls[2].init.method, "POST");
  assert.equal(calls[2].init.body, undefined);

  await api.api.me();
  assert.equal(calls[3].href, "/auth/me");
  assert.equal(calls[3].init.method, "GET");

  await api.api.verifyEmail("vtok");
  assert.equal(calls[4].href, "/auth/verify-email");
  assert.deepEqual(JSON.parse(calls[4].init.body), { token: "vtok" });

  await api.api.resendVerification();
  assert.equal(calls[5].href, "/auth/resend-verification");
  assert.equal(calls[5].init.method, "POST");
  assert.equal(calls[5].init.body, undefined);

  await api.api.forgotPassword("j@acme.test");
  assert.equal(calls[6].href, "/auth/forgot-password");
  assert.deepEqual(JSON.parse(calls[6].init.body), { email: "j@acme.test" });

  await api.api.resetPassword("rtok", "newpw");
  assert.equal(calls[7].href, "/auth/reset-password");
  assert.deepEqual(JSON.parse(calls[7].init.body), { token: "rtok", password: "newpw" });

  await api.api.changePassword("old", "new");
  assert.equal(calls[8].href, "/auth/change-password");
  assert.deepEqual(JSON.parse(calls[8].init.body), { current_password: "old", new_password: "new" });

  await api.api.updateSettings({ weekly_digest_enabled: false });
  assert.equal(calls[9].href, "/auth/settings");
  assert.equal(calls[9].init.method, "PATCH");
  assert.deepEqual(JSON.parse(calls[9].init.body), { weekly_digest_enabled: false });
});

test("tools lists the agent registry", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ count: 2, tools: [] }), { headers: json }));
  await api.api.tools();
  assert.equal(calls[0].href, "/tools");
  assert.equal(calls[0].init.method, "GET");
  assert.equal(calls[0].init.body, undefined);
});

test("overview is the dashboard's single call", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ shops: [], competitor_count: 0 }), { headers: json }));
  await api.api.overview();
  assert.equal(calls[0].href, "/overview");
  assert.equal(calls[0].init.method, "GET");
});

test("catalog methods hit their routes", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ count: 0, products: [] }), { headers: json }));
  await api.api.analytics();
  assert.equal(calls[0].href, "/analytics/summary");
  assert.equal(calls[0].init.method, "GET");

  await api.api.products("shoes", 25);
  assert.equal(calls[1].href, "/products?limit=25&search=shoes");

  await api.api.products(null);
  assert.equal(calls[2].href, "/products?limit=50");

  await api.api.product(9);
  assert.equal(calls[3].href, "/products/9");

  await api.api.discoverCompetitors(4);
  assert.equal(calls[4].href, "/shops/4/discover");
  assert.equal(calls[4].init.method, "POST");
  assert.equal(calls[4].init.body, undefined);

  await api.api.shopReports(4);
  assert.equal(calls[5].href, "/shops/4/reports");
  assert.equal(calls[5].init.method, "GET");

  await api.api.generateReport(4, 14);
  assert.equal(calls[6].href, "/shops/4/report?days=14");
  assert.equal(calls[6].init.method, "POST");

  await api.api.generateReport(4);
  assert.equal(calls[7].href, "/shops/4/report?days=7");
});

test("competitor methods hit their routes", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ count: 0, competitors: [] }), { headers: json }));
  await api.api.competitors(4);
  assert.equal(calls[0].href, "/competitors?shop_id=4");
  assert.equal(calls[0].init.method, "GET");

  await api.api.competitors();
  assert.equal(calls[1].href, "/competitors");

  await api.api.createCompetitor({ shop_id: 4, name: "Rival", url: "https://rival.test" });
  assert.equal(calls[2].href, "/competitors");
  assert.equal(calls[2].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[2].init.body), { shop_id: 4, name: "Rival", url: "https://rival.test" });

  await api.api.deleteCompetitor(7);
  assert.equal(calls[3].href, "/competitors/7");
  assert.equal(calls[3].init.method, "DELETE");
  assert.equal(calls[3].init.body, undefined);

  await api.api.crawlCompetitor(7);
  assert.equal(calls[4].href, "/competitors/7/crawl");
  assert.equal(calls[4].init.method, "POST");
});

test("change methods hit their routes", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ count: 0, unacknowledged: 0, changes: [] }), { headers: json }));
  await api.api.changes({ severity: "high" });
  assert.equal(calls[0].href, "/changes?days=30&limit=50&severity=high");

  await api.api.acknowledgeChange(11);
  assert.equal(calls[1].href, "/changes/11/ack");
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[1].init.body, undefined);

  await api.api.changeHistory(12);
  assert.equal(calls[2].href, "/changes/12/history");
  assert.equal(calls[2].init.method, "GET");
});

test("report methods hit their routes", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ count: 0, reports: [] }), { headers: json }));
  await api.api.reports(5);
  assert.equal(calls[0].href, "/reports?limit=5");
  assert.equal(calls[0].init.method, "GET");

  await api.api.report(3);
  assert.equal(calls[1].href, "/reports/3");
  assert.equal(calls[1].init.method, "GET");
});

test("billing methods hit their routes", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ plans: [], trial_days: 14, currency: "eur" }), { headers: json }));
  await api.api.plans();
  assert.equal(calls[0].href, "/billing/plans");
  assert.equal(calls[0].init.method, "GET");

  await api.api.plan();
  assert.equal(calls[1].href, "/billing/plan");
  assert.equal(calls[1].init.method, "GET");

  await api.api.checkout("pro");
  assert.equal(calls[2].href, "/billing/checkout");
  assert.equal(calls[2].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[2].init.body), { plan: "pro" });

  await api.api.portal();
  assert.equal(calls[3].href, "/billing/portal");
  assert.equal(calls[3].init.method, "POST");
  assert.equal(calls[3].init.body, undefined);

  await api.api.usage(14);
  assert.equal(calls[4].href, "/billing/usage?days=14");
  assert.equal(calls[4].init.method, "GET");

  await api.api.usage();
  assert.equal(calls[5].href, "/billing/usage?days=30");
});

test("agent methods hit their routes", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ run_id: 1, status: "queued" }), { headers: json }));
  await api.api.runs(20);
  assert.equal(calls[0].href, "/agent/runs?limit=20");
  assert.equal(calls[0].init.method, "GET");

  await api.api.run("r-1");
  assert.equal(calls[1].href, "/agent/runs/r-1");

  await api.api.startRun("check prices", "web");
  assert.equal(calls[2].href, "/agent/run");
  assert.equal(calls[2].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[2].init.body), { message: "check prices", session_id: "web" });

  await api.api.approvals("pending");
  assert.equal(calls[3].href, "/approvals?status_filter=pending");

  await api.api.approvals();
  assert.equal(calls[4].href, "/approvals");

  await api.api.resolveApproval(5, true, "looks right");
  assert.equal(calls[5].href, "/approvals/5/resolve");
  assert.deepEqual(JSON.parse(calls[5].init.body), { approved: true, note: "looks right" });
});

test("admin methods hit their routes", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ count: 0, users: [] }), { headers: json }));
  await api.api.users();
  assert.equal(calls[0].href, "/admin/users");
  assert.equal(calls[0].init.method, "GET");

  await api.api.patchUser(2, { role: "viewer" });
  assert.equal(calls[1].href, "/admin/users/2");
  assert.equal(calls[1].init.method, "PATCH");
  assert.deepEqual(JSON.parse(calls[1].init.body), { role: "viewer" });
});

test("knowledge methods hit their routes", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ count: 0 }), { headers: json }));
  await api.api.documents(10);
  assert.equal(calls[0].href, "/documents?limit=10");

  await api.api.documents();
  assert.equal(calls[1].href, "/documents?limit=50");

  const files = [new File(["a"], "a.pdf"), new File(["b"], "b.txt")];
  await api.api.uploadDocuments(files);
  assert.equal(calls[2].href, "/documents/upload");
  assert.equal(calls[2].init.method, "POST");
  assert.ok(calls[2].init.body instanceof FormData);
  const uploaded = calls[2].init.body.getAll("files");
  assert.equal(uploaded.length, 2);
  assert.deepEqual([...uploaded], files);

  await api.api.deleteDocument("doc/1");
  assert.equal(calls[3].href, "/documents/doc%2F1");
  assert.equal(calls[3].init.method, "DELETE");

  await api.api.ragStatus();
  assert.equal(calls[4].href, "/rag/status");

  await api.api.chat("hello");
  assert.equal(calls[5].href, "/chat");
  assert.deepEqual(JSON.parse(calls[5].init.body), { message: "hello", use_rag: true });

  await api.api.chat("hello again", "c-9", false);
  assert.equal(calls[6].href, "/chat");
  assert.deepEqual(JSON.parse(calls[6].init.body), { message: "hello again", use_rag: false, conversation_id: "c-9" });

  await api.api.clearConversation("c 9");
  assert.equal(calls[7].href, "/conversations/c%209/history");
  assert.equal(calls[7].init.method, "DELETE");

  await api.api.prompts();
  assert.equal(calls[8].href, "/prompts");
});

test("intelligence methods hit their routes", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ count: 0 }), { headers: json }));
  await api.api.startResearch("find shoe stores", ["https://a.test"]);
  assert.equal(calls[0].href, "/intelligence/jobs");
  assert.equal(calls[0].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].init.body), { query: "find shoe stores", start_urls: ["https://a.test"] });

  await api.api.researchJobs(5);
  assert.equal(calls[1].href, "/intelligence/jobs?limit=5");

  await api.api.researchJobs();
  assert.equal(calls[2].href, "/intelligence/jobs?limit=20");

  await api.api.stores(10);
  assert.equal(calls[3].href, "/intelligence/stores?limit=10");

  await api.api.opportunities("gap", 25);
  assert.equal(calls[4].href, "/intelligence/opportunities?limit=25&kind=gap");

  await api.api.opportunities();
  assert.equal(calls[5].href, "/intelligence/opportunities?limit=50");

  await api.api.opportunity("op 1");
  assert.equal(calls[6].href, "/intelligence/opportunities/op%201");
});

test("create methods hit their routes", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ ok: true }), { headers: json }));
  await api.api.comfyStatus();
  assert.equal(calls[0].href, "/comfyui/status");

  await api.api.workflows();
  assert.equal(calls[1].href, "/comfyui/workflows");

  await api.api.validateGeneration({ prompt: "sneaker" });
  assert.equal(calls[2].href, "/comfyui/validate-generation");
  assert.equal(calls[2].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[2].init.body), { prompt: "sneaker" });

  await api.api.generate({ workflow: "sd", seed: 1 });
  assert.equal(calls[3].href, "/comfyui/generate");
  assert.deepEqual(JSON.parse(calls[3].init.body), { workflow: "sd", seed: 1 });

  await api.api.generated(10);
  assert.equal(calls[4].href, "/generated?limit=10");

  await api.api.hardware();
  assert.equal(calls[5].href, "/training/hardware");

  await api.api.datasets();
  assert.equal(calls[6].href, "/datasets");

  await api.api.createDataset({ name: "sneakers" });
  assert.equal(calls[7].href, "/datasets");
  assert.equal(calls[7].init.method, "POST");
  assert.deepEqual(JSON.parse(calls[7].init.body), { name: "sneakers" });

  await api.api.validateDataset("d 1");
  assert.equal(calls[8].href, "/datasets/d%201/validate");

  await api.api.trainingStatus();
  assert.equal(calls[9].href, "/training/status");

  await api.api.trainingPresets();
  assert.equal(calls[10].href, "/training/presets");

  await api.api.trainingProjects();
  assert.equal(calls[11].href, "/training/projects");
});

test("remaining shop and system methods hit their routes", async () => {
  resetEnv();
  const calls = capture(() => new Response(JSON.stringify({ ok: true }), { headers: json }));
  await api.api.tools();
  assert.equal(calls[0].href, "/tools");
  assert.equal(calls[0].init.method, "GET");

  await api.api.discoverCompetitors(3);
  assert.equal(calls[1].href, "/shops/3/discover");
  assert.equal(calls[1].init.method, "POST");
  assert.equal(calls[1].init.body, undefined);

  await api.api.shopReports(3);
  assert.equal(calls[2].href, "/shops/3/reports");
  assert.equal(calls[2].init.method, "GET");

  await api.api.generateReport(3, 14);
  assert.equal(calls[3].href, "/shops/3/report?days=14");
  assert.equal(calls[3].init.method, "POST");
});


