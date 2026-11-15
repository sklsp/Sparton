// knowledge.js on top of the DOM fakes: RAG stats, the document library and its filter, grounded chat
// with citations, and the prompt template list. Fixtures mirror the API shapes in app/api/knowledge.py
// (list_documents, rag_status, ChatResponse). Not covered yet: file upload, document delete, the
// use-documents toggle, chat failure and New chat; they need file inputs and getElementById in dom.mjs.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then DOM fakes; both install onto globalThis at import time.
import knowledgeView from "../../app/web/views/knowledge.js";
import { bytes, num, ago } from "../../app/web/ui.js";
import { t } from "../../app/web/i18n.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom, Event } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// Fixtures mirror the shapes in app/api/knowledge.py.
const docA = { id: 1, title: "Brand Guidelines.pdf", size_bytes: 2_457_600, chunks: 38, created_at: new Date(Date.now() - 2 * 36e5).toISOString() };
const docB = { id: 2, title: "Onboarding Notes.md", size_bytes: 4_812, chunks: 3, created_at: new Date(Date.now() - 90 * 6e4).toISOString() };
const docsBody = { count: 2, documents: [docA, docB] };
const ragBody = { chunks: 41, documents: 2, embedding_backend: "openai", embedding_model: "text-embedding-3-small" };
const promptsBody = { count: 1, prompts: [{ id: 7, key: "support_reply", name: "Support reply tone", description: "Warm, concise, on-brand." }] };
const chatAnswer = (id) => ({ conversation_id: id, answer: "Our policy is 30 days.", citations: [{ document: "Brand Guidelines.pdf", chunk: 4, excerpt: "Refunds within 30 days of purchase." }] });

function renderKnowledge({ docs = docsBody, rag = ragBody, prompts = promptsBody, failRag = false, failUpload = false, uploadResult = null, deleteResponse = null, chatResponses = [chatAnswer(11)], docFailuresLeft = 0, promptFailuresLeft = 0 } = {}) {
  resetEnv();
  resetDom();
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  const chats = [];
  const uploads = [];
  let docFails = docFailuresLeft;
  let promptFails = promptFailuresLeft;
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (method === "GET" && href.startsWith("/documents?")) {
      // The stats panel loads with limit=200 and the library list with limit=100; only the list can fail in tests.
      const limit = Number(new URLSearchParams(href.split("?")[1]).get("limit"));
      if (limit === 100 && docFails > 0) { docFails -= 1; return json({ detail: "Documents unavailable" }, 503); }
      return json(docs);
    }
    if (method === "GET" && href === "/rag/status") return failRag ? json({ detail: "index down" }, 500) : json(rag);
    if (method === "GET" && href === "/prompts") {
      if (promptFails > 0) { promptFails -= 1; return json({ detail: "Templates unavailable" }, 503); }
      return json(prompts);
    }
    if (method === "POST" && href === "/documents/upload") {
      uploads.push(init.body);
      if (failUpload) return json({ detail: "Storage backend down" }, 502);
      return json(uploadResult ?? { uploaded: 2, failed: 0, results: [{ filename: "a.txt", id: 3, chunks: 5 }, { filename: "b.pdf", id: 4, chunks: 9 }] });
    }
    const del = /^\/documents\/(\d+)$/.exec(href);
    if (method === "DELETE" && del) return deleteResponse ?? json({ deleted: true });
    if (method === "POST" && href === "/chat") {
      chats.push(JSON.parse(init.body));
      const next = chatResponses.shift() ?? chatAnswer(12);
      return next instanceof Response ? next : json(next);
    }
    if (method === "DELETE" && /^\/conversations\/\d+\/history$/.test(href)) return json({ cleared: true });
    throw new Error(`unexpected fetch ${method} ${href}`);
  });
  knowledgeView(host);
  return { host, calls, chats, uploads };
}

const waitFor = async (fn, what) => {
  const deadline = Date.now() + 5000;
  for (;;) {
    if (fn()) return;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((resolve) => setImmediate(resolve));
  }
};

const flush = () => new Promise((resolve) => { let n = 0; const tick = () => (++n < 50 ? setImmediate(tick) : resolve()); setImmediate(tick); });

// No panel is still showing a skeleton.
const settled = (host) => !host.querySelector("[aria-busy]");

// Section order inside the view: documents card, chat panel, prompts card.
const docsCard = (host) => host.querySelectorAll("section.card")[0];
const chatPanelEl = (host) => host.querySelectorAll("section.card")[1];
const promptsCard = (host) => host.querySelectorAll("section.card")[2];

test("renders the header and loads stats, library, chat and prompts", async () => {
  const { host, calls } = renderKnowledge();
  await waitFor(() => settled(host), "all panels to settle");
  assert.equal(host.querySelector("h1").textContent, "Knowledge");
  assert.equal(host.querySelector(".page-head p").textContent, "Documents indexed for retrieval. Chat answers cite the chunks they used.");
  const gets = [...new Set(calls.filter(([m]) => m === "GET").map(([, h]) => h))].sort();
  assert.deepEqual(gets, ["/documents?limit=100", "/documents?limit=200", "/prompts", "/rag/status"]);
  const headings = [...host.querySelectorAll("section.card h2")].map((h) => h.textContent);
  assert.deepEqual(headings, ["Documents", "Chat", "Prompt templates"]);
  assert.equal(host.querySelector(".error-state"), null);
});

test("stats show document totals and the embedding backend", async () => {
  const { host } = renderKnowledge();
  await waitFor(() => settled(host), "stats to settle");
  const metrics = [...host.querySelectorAll(".grid-metrics .metric")];
  assert.deepEqual(metrics.map((m) => m.querySelector(".metric-label").textContent), ["Documents", "Indexed chunks", "Embeddings"]);
  assert.deepEqual(metrics.map((m) => m.querySelector(".metric-value").textContent), [num(docsBody.count), num(ragBody.chunks), ragBody.embedding_backend]);
  const totalBytes = docA.size_bytes + docB.size_bytes;
  assert.deepEqual(metrics.map((m) => m.querySelector(".metric-sub").textContent), [bytes(totalBytes), `${num(ragBody.documents)} versions tracked`, ragBody.embedding_model]);
});

test("a dead RAG index shows a warning banner and placeholder metrics", async () => {
  const { host } = renderKnowledge({ failRag: true });
  await waitFor(() => settled(host), "stats to settle");
  const banner = host.querySelector(".banner[data-tone=warning]");
  assert.ok(banner, "the warning banner renders");
  assert.match(banner.textContent, /chat cannot cite them/);
  const metrics = [...host.querySelectorAll(".grid-metrics .metric")];
  assert.equal(metrics[1].querySelector(".metric-value").textContent, "0");
  assert.equal(metrics[1].querySelector(".metric-sub").textContent, "index unavailable");
  assert.equal(metrics[2].querySelector(".metric-value").textContent, "—");
  assert.equal(metrics[2].querySelector(".metric-sub").textContent, "no model reported");
});

test("the library lists documents with size, chunk count and age", async () => {
  const { host } = renderKnowledge();
  await waitFor(() => settled(host), "library to settle");
  const card = docsCard(host);
  const rows = [...card.querySelectorAll(".rows .row")];
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((r) => r.querySelector(".row-title").textContent), [docA.title, docB.title]);
  for (const [i, doc] of [docA, docB].entries()) {
    const row = rows[i];
    assert.equal(row.querySelector(".row-title").getAttribute("title"), doc.title);
    assert.equal(row.querySelector(".row-sub").textContent, `${bytes(doc.size_bytes)} · ${num(doc.chunks)} chunks · ${ago(doc.created_at)}`);
    assert.equal(row.querySelector("button.icon-btn").getAttribute("aria-label"), `Delete ${doc.title}`);
  }
});

test("filtering narrows the list and shows a no-match plate", async () => {
  const { host } = renderKnowledge();
  await waitFor(() => settled(host), "library to settle");
  const card = docsCard(host);
  const search = card.querySelector(".search input");
  search.value = "brand";
  search.dispatchEvent(new Event("input"));
  let rows = [...card.querySelectorAll(".rows .row")];
  assert.deepEqual(rows.map((r) => r.querySelector(".row-title").textContent), [docA.title]);
  search.value = "zzz";
  search.dispatchEvent(new Event("input"));
  const plate = card.querySelector(".empty");
  assert.equal(plate.querySelector("h3").textContent, "No matches");
  assert.match(plate.textContent, /Nothing matches “zzz”/);
});

test("an empty library shows the upload plate", async () => {
  const { host, calls } = renderKnowledge({ docs: { count: 0, documents: [] } });
  await waitFor(() => settled(host), "library to settle");
  const card = docsCard(host);
  const plate = card.querySelector(".empty");
  assert.equal(plate.querySelector("h3").textContent, "No documents yet");
  const upload = [...card.querySelectorAll("button")].find((b) => b.textContent === "Upload documents");
  assert.ok(upload, "the upload action renders");
  upload.click(); // opens the file picker; without a change event nothing is sent
  await flush();
  assert.equal(calls.filter(([m, h]) => m === "POST" && h === "/documents/upload").length, 0);
});

test("cancelling the delete dialog makes no request", async () => {
  const { host, calls } = renderKnowledge();
  await waitFor(() => settled(host), "library to settle");
  docsCard(host).querySelectorAll("button.icon-btn")[0].click();
  const [cancel] = [...document.querySelectorAll(".dialog-foot button")];
  cancel.click();
  assert.equal(document.querySelector("dialog"), null);
  assert.equal(calls.filter(([m]) => m === "DELETE").length, 0);
});

test("a failed delete toasts the API error and keeps the list", async () => {
  const { host, calls } = renderKnowledge({ deleteResponse: json({ detail: "Document not found" }, 404) });
  await waitFor(() => settled(host), "library to settle");
  docsCard(host).querySelectorAll("button.icon-btn")[0].click();
  [...document.querySelectorAll(".dialog-foot button")][1].click();
  await waitFor(() => document.querySelector('.toast[data-tone="danger"]'), "the error toast");
  assert.match(document.querySelector('.toast[data-tone="danger"]').textContent, /Document not found/);
  assert.equal(calls.filter(([m, h]) => m === "GET" && h === "/documents?limit=100").length, 1);
});

test("chat answers with citations and continues the conversation", async () => {
  const { host, chats } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  const form = chatPanelEl(host).querySelector("form.chat-form");
  const input = form.querySelector("textarea.textarea");
  input.value = "What is our refund policy?";
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => host.querySelector('.msg[data-role="assistant"] .msg-text')?.textContent.includes("Our policy"), "the assistant reply");
  const msgs = [...host.querySelectorAll(".chat-log .msg")];
  assert.equal(msgs.length, 2);
  assert.equal(msgs[0].querySelector(".msg-avatar").textContent, "you");
  assert.equal(msgs[0].querySelector(".msg-text").textContent, "What is our refund policy?");
  assert.equal(msgs[1].querySelector(".msg-avatar").textContent, "sp");
  const chip = msgs[1].querySelector(".msg-cites .chip");
  assert.equal(chip.textContent, "[1] Brand Guidelines.pdf #4");
  assert.equal(chip.getAttribute("title"), "Refunds within 30 days of purchase.");
  assert.deepEqual(chats[0], { message: "What is our refund policy?", use_rag: true });

  input.value = "Any exceptions?";
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => chats.length === 2 && host.querySelectorAll(".chat-log .msg").length === 4, "the second exchange");
  assert.equal(chats[1].conversation_id, 11);
});

test("prompt templates render name, description and key chip", async () => {
  const { host } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  const card = promptsCard(host);
  const rows = [...card.querySelectorAll(".rows .row")];
  assert.equal(rows.length, 1);
  assert.equal(rows[0].querySelector(".row-title").textContent, "Support reply tone");
  assert.equal(rows[0].querySelector(".row-sub").textContent, "Warm, concise, on-brand.");
  const chip = rows[0].querySelector("span.chip");
  assert.equal(chip.textContent, "support_reply");

  const { host: bare } = renderKnowledge({ prompts: { count: 1, prompts: [{ id: 8, key: "x", name: "X", description: null }] } });
  await waitFor(() => settled(bare), "panels to settle");
  assert.equal(promptsCard(bare).querySelector(".row-sub").textContent, "No description");
});

test("an empty template list shows the no-templates plate", async () => {
  const { host } = renderKnowledge({ prompts: { count: 0, prompts: [] } });
  await waitFor(() => settled(host), "panels to settle");
  const plate = promptsCard(host).querySelector(".empty");
  assert.equal(plate.querySelector("h3").textContent, "No prompt templates");
});

test("a failed documents load shows an error state whose retry recovers", async () => {
  const { host } = renderKnowledge({ docFailuresLeft: 1 });
  await waitFor(() => docsCard(host).querySelector(".error-state"), "the error state");
  const err = docsCard(host).querySelector(".error-state");
  assert.equal(err.getAttribute("role"), "alert");
  assert.equal(err.querySelector("h3").textContent, t("ui.error"));
  assert.equal(err.querySelector("p").textContent, "Documents unavailable");
  const retry = [...err.querySelectorAll("button")].find((b) => b.textContent === t("ui.retry"));
  assert.ok(retry, "the retry button renders");
  retry.click();
  await waitFor(() => docsCard(host).querySelectorAll(".rows .row").length === 2, "the recovered list");
});

test("a failed prompts load shows an error state whose retry recovers", async () => {
  const { host } = renderKnowledge({ promptFailuresLeft: 1 });
  await waitFor(() => promptsCard(host).querySelector(".error-state"), "the error state");
  const err = promptsCard(host).querySelector(".error-state");
  assert.equal(err.querySelector("p").textContent, "Templates unavailable");
  [...err.querySelectorAll("button")].find((b) => b.textContent === t("ui.retry")).click();
  await waitFor(() => promptsCard(host).querySelectorAll(".rows .row").length === 1, "the recovered list");
});
