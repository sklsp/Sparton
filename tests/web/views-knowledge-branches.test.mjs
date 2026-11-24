// knowledge.js branch coverage: the fallbacks and wiring neither main suite exercises: an upload
// result with no results list, documents/prompts payloads missing their lists, a chat answer without
// citations at all, and the Upload button's click forwarding to the hidden picker.
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then DOM fakes; both install onto globalThis at import time.
import knowledgeView from "../../app/web/views/knowledge.js";
import { resetToasts } from "../../app/web/ui.js";
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom, Event } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// Fixtures mirror the shapes in app/api/knowledge.py.
const docsBody = { count: 1, documents: [{ id: 1, title: "Brand Guidelines.pdf", size_bytes: 2_457_600, chunks: 38, created_at: new Date(Date.now() - 2 * 36e5).toISOString() }] };
const ragBody = { chunks: 41, documents: 1, embedding_backend: "openai", embedding_model: "text-embedding-3-small" };
const promptsBody = { count: 1, prompts: [{ id: 7, key: "support_reply", name: "Support reply tone", description: "Warm, concise, on-brand." }] };
const chatAnswer = (id) => ({ conversation_id: id, answer: "Our policy is 30 days.", citations: [{ document: "Brand Guidelines.pdf", chunk: 4, excerpt: "Refunds within 30 days of purchase." }] });

function renderKnowledge({ docs = docsBody, prompts = promptsBody, uploadResult = null, chatResponses = [chatAnswer(21)] } = {}) {
  resetEnv();
  resetDom();
  resetToasts(); // ui.js caches the toasts host per document; a fresh body gets its own node.
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  const chats = [];
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (method === "GET" && href.startsWith("/documents?")) return json(docs);
    if (method === "GET" && href === "/rag/status") return json(ragBody);
    if (method === "GET" && href === "/prompts") return json(prompts);
    // The default result has no `results` key: the view's `(result.results || [])` fallback is what these tests pin.
    if (method === "POST" && href === "/documents/upload") return json(uploadResult ?? { uploaded: 2, failed: 0 });
    if (method === "POST" && href === "/chat") { chats.push(JSON.parse(init.body)); const next = chatResponses.shift() ?? chatAnswer(12); return next instanceof Response ? next : json(next); }
    throw new Error(`unexpected fetch ${method} ${href}`);
  });
  knowledgeView(host);
  return { host, calls, chats };
}

const waitFor = async (predicate, label) => {
  const deadline = Date.now() + 2000;
  for (;;) {
    if (predicate()) return;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
};

// No panel is still showing a skeleton.
const settled = (host) => !host.querySelector("[aria-busy]");
// Section order inside the view: documents card, chat panel, prompts card.
const docsCard = (host) => host.querySelectorAll("section.card")[0];
const chatPanelEl = (host) => host.querySelectorAll("section.card")[1];
const promptsCard = (host) => host.querySelectorAll("section.card")[2];

// Replace the hidden picker's click() with a counter so a test can prove a button forwarded to it.
const watchPickerClicks = (fileInput) => {
  let clicks = 0;
  fileInput.click = () => { clicks += 1; };
  return () => clicks;
};

test("an upload result without a results list still toasts the count and reloads", async () => {
  const { host, calls } = renderKnowledge({ uploadResult: { uploaded: 2, failed: 0 } }); // no `results` key at all
  await waitFor(() => settled(host), "panels to settle");
  const fileInput = docsCard(host).querySelector(".card-head-actions input");
  fileInput.setFiles([new File(["one"], "a.txt"), new File(["two"], "b.pdf")]);
  fileInput.dispatchEvent(new Event("change"));
  await waitFor(() => document.querySelector('.toast[data-tone="success"]'), "the success toast");
  assert.equal(document.querySelector('.toast[data-tone="success"]').textContent, "Indexed 2 documents");
  assert.equal(document.querySelectorAll(".toast").length, 1); // no per-file toasts: nothing in the missing list to iterate
  await waitFor(() => settled(host), "panels to settle again");
  assert.equal(calls.filter(([m, h]) => m === "GET" && h === "/documents?limit=100").length, 2); // initial load + reload after upload
  assert.equal(fileInput.value, ""); // finally still clears the picker
});

test("a documents payload without a list renders the empty library plate", async () => {
  const { host } = renderKnowledge({ docs: { count: 0 } }); // no `documents` key at all
  await waitFor(() => settled(host), "panels to settle");
  const card = docsCard(host);
  const plate = card.querySelector(".empty");
  assert.equal(plate.querySelector("h3").textContent, "No documents yet");
  // the stats panel sums over the same fallback: zero bytes under the count metric
  const metrics = [...host.querySelectorAll(".grid-metrics .metric")];
  assert.equal(metrics[0].querySelector(".metric-value").textContent, "0");
  assert.equal(metrics[0].querySelector(".metric-sub").textContent, "0 B");
});

test("the Upload button forwards its click to the hidden file picker", async () => {
  const { host } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  const card = docsCard(host);
  const fileInput = card.querySelector(".card-head-actions input");
  const clicks = watchPickerClicks(fileInput);
  const uploadBtn = [...card.querySelectorAll("button")].find((b) => b.textContent === "Upload");
  assert.ok(uploadBtn, "the Upload button renders");
  uploadBtn.click();
  assert.equal(clicks(), 1); // the click lands on fileInput.click()
});

test("a prompts payload without a list shows the no-templates plate", async () => {
  const { host } = renderKnowledge({ prompts: { count: 0 } }); // no `prompts` key at all
  await waitFor(() => settled(host), "panels to settle");
  const plate = promptsCard(host).querySelector(".empty");
  assert.equal(plate.querySelector("h3").textContent, "No prompt templates");
});

test("an answer without citations renders the message with no chip row", async () => {
  const { host } = renderKnowledge({ chatResponses: [{ conversation_id: 31, answer: "Just a plain reply." }] }); // no `citations` key at all
  await waitFor(() => settled(host), "panels to settle");
  const form = chatPanelEl(host).querySelector("form.chat-form");
  form.querySelector("textarea.textarea").value = "Anything?";
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => host.querySelector('.msg[data-role="assistant"] .msg-text')?.textContent.includes("plain reply"), "the assistant reply");
  const msg = [...host.querySelectorAll(".chat-log .msg")].at(-1);
  assert.equal(msg.querySelector(".msg-cites"), null); // no citations key: the chip row is skipped entirely
});

