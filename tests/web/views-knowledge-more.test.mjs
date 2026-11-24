// knowledge.js paths the first suite leaves open: file upload (success, mixed results, all
// failed, API error and an empty change), document delete success, the use-documents toggle,
// chat failure, New chat with and without a conversation, blank submits, Enter handling,
// textarea autosize and citation fallbacks. Fixtures mirror the API shapes in
// app/api/knowledge.py (upload_documents, list_documents, ChatResponse, clear_conversation).
import { test } from "node:test";
import assert from "node:assert/strict";
import "./env.mjs"; // browser fakes first...
import "./dom.mjs"; // ...then DOM fakes; both install onto globalThis at import time.
import knowledgeView from "../../app/web/views/knowledge.js";
import { resetToasts } from "../../app/web/ui.js"; // toast() caches its host; drop it with the body
import { resetEnv, setFetch } from "./env.mjs";
import { resetDom, Event } from "./dom.mjs";

const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// Fixtures mirror the shapes in app/api/knowledge.py.
const docsBody = {
  count: 2,
  documents: [
    { id: 1, title: "Brand Guidelines.pdf", size_bytes: 48000, chunks: 6, created_at: "2026-11-20T09:00:00" },
    { id: 2, title: "Onboarding Notes.md", size_bytes: 3500, chunks: 2, created_at: "2026-11-21T14:30:00" },
  ],
};
const ragBody = { ready: true, chunks: 8, documents: 2, embedding_backend: "local", embedding_model: "all-MiniLM-L6-v2" };
const promptsBody = { count: 1, prompts: [{ id: 1, key: "summarise", name: "Summarise", description: "" }] };

const chatAnswer = (conversation_id) => ({
  conversation_id,
  answer: "Our policy is in the guidelines.",
  citations: [{ document: "Brand Guidelines.pdf", chunk: 2, excerpt: "Refunds within 30 days." }],
});

function renderKnowledge({ docs = docsBody, uploadResult = null, failUpload = false, deleteResponse = null, chatResponses = [chatAnswer(21)], clearResponse = json({ cleared: true }) } = {}) {
  resetEnv();
  resetDom();
  resetToasts(); // toast() caches its host across tests; the fresh body needs a fresh one
  const host = document.createElement("main");
  document.body.appendChild(host);
  const calls = [];
  const chats = [];
  const uploads = [];
  setFetch((href, init) => {
    const method = init?.method ?? "GET";
    calls.push([method, href]);
    if (method === "GET" && href.startsWith("/documents?")) return json(docs);
    if (method === "GET" && href === "/rag/status") return json(ragBody);
    if (method === "GET" && href === "/prompts") return json(promptsBody);
    if (method === "POST" && href === "/documents/upload") {
      uploads.push(init.body); // the FormData the view built from fileInput.files
      if (failUpload) return json({ detail: "Storage backend down" }, 502);
      return json(uploadResult ?? { uploaded: 2, failed: 0, results: [] });
    }
    const del = /^\/documents\/(\d+)$/.exec(href);
    if (method === "DELETE" && del) return deleteResponse ?? json({ deleted: true });
    if (method === "POST" && href === "/chat") {
      chats.push(JSON.parse(init.body));
      const next = chatResponses.shift() ?? chatAnswer(22);
      return next instanceof Response ? next : json(next);
    }
    if (method === "DELETE" && /^\/conversations\/\d+\/history$/.test(href)) return clearResponse;
    throw new Error(`unexpected fetch ${method} ${href}`);
  });
  knowledgeView(host);
  return { host, calls, chats, uploads };
}

const waitFor = async (predicate, label) => {
  const deadline = Date.now() + 2000;
  for (;;) {
    if (predicate()) return;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
};

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));
const settled = (host) => !host.querySelector("[aria-busy]");
const docsCard = (host) => host.querySelectorAll("section.card")[0];
const chatPanelEl = (host) => host.querySelectorAll("section.card")[1];

test("picking files uploads them, toasts the count and reloads library and stats", async () => {
  const { host, calls, uploads } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  const fileInput = docsCard(host).querySelector(".card-head-actions input");
  assert.equal(fileInput.getAttribute("accept"), ".pdf,.txt,.md,.docx,.doc,.csv,.json"); // the hidden picker, not a text field
  fileInput.setFiles([new File(["one"], "a.txt"), new File(["two"], "b.pdf")]);
  fileInput.dispatchEvent(new Event("change"));
  await waitFor(() => document.querySelector('.toast[data-tone="success"]'), "the success toast");
  assert.equal(document.querySelector('.toast[data-tone="success"]').textContent, "Indexed 2 documents"); // plural
  const sent = uploads[0].getAll("files");
  assert.deepEqual(sent.map((f) => f.name), ["a.txt", "b.pdf"]); // both files went into the FormData
  assert.equal(uploads.length, 1);
  assert.equal(fileInput.value, ""); // finally: the picker is cleared even on success
  const listLoads = calls.filter(([m, h]) => m === "GET" && h === "/documents?limit=100").length;
  const statsLoads = calls.filter(([m, h]) => m === "GET" && h === "/documents?limit=200").length;
  assert.equal(listLoads, 2); // initial load + reload after upload
  assert.equal(statsLoads, 2);
});

test("a mixed upload toasts the indexed total and each failed file", async () => {
  const result = {
    uploaded: 1,
    failed: 1,
    results: [
      { filename: "a.txt", id: 5, chunks: 2 },
      { filename: "bad.exe", error: "Unsupported file type" },
    ],
  };
  const { host } = renderKnowledge({ uploadResult: result });
  await waitFor(() => settled(host), "panels to settle");
  docsCard(host).querySelector(".card-head-actions input").setFiles([new File(["one"], "a.txt"), new File(["x"], "bad.exe")]);
  docsCard(host).querySelector(".card-head-actions input").dispatchEvent(new Event("change"));
  await waitFor(() => document.querySelectorAll(".toast").length === 2, "both toasts");
  const texts = [...document.querySelectorAll(".toast")].map((el) => el.textContent);
  assert.ok(texts.includes("Indexed 1 document"), `singular count toast missing in ${JSON.stringify(texts)}`);
  assert.ok(texts.includes("bad.exe: Unsupported file type"), `per-file failure toast missing in ${JSON.stringify(texts)}`);
});

test("an all-failed upload toasts only the failures", async () => {
  const result = {
    uploaded: 0,
    failed: 2,
    results: [
      { filename: "a.exe", error: "Unsupported file type" },
      { filename: "b.bin", error: "File too large" },
    ],
  };
  const { host } = renderKnowledge({ uploadResult: result });
  await waitFor(() => settled(host), "panels to settle");
  docsCard(host).querySelector(".card-head-actions input").setFiles([new File(["x"], "a.exe"), new File(["y"], "b.bin")]);
  docsCard(host).querySelector(".card-head-actions input").dispatchEvent(new Event("change"));
  await waitFor(() => document.querySelectorAll('.toast[data-tone="danger"]').length === 2, "both failure toasts");
  const texts = [...document.querySelectorAll(".toast")].map((el) => el.textContent);
  assert.ok(texts.includes("a.exe: Unsupported file type"), `missing in ${JSON.stringify(texts)}`);
  assert.ok(texts.includes("b.bin: File too large"), `missing in ${JSON.stringify(texts)}`);
  assert.equal(document.querySelector('.toast[data-tone="success"]'), null); // nothing indexed, no success toast
});

test("a failed upload toasts the API error without reloading", async () => {
  const { host, calls } = renderKnowledge({ failUpload: true });
  await waitFor(() => settled(host), "panels to settle");
  const fileInput = docsCard(host).querySelector(".card-head-actions input");
  fileInput.setFiles([new File(["one"], "a.txt")]);
  fileInput.dispatchEvent(new Event("change"));
  await waitFor(() => document.querySelector('.toast[data-tone="danger"]'), "the error toast");
  assert.equal(document.querySelector('.toast[data-tone="danger"]').textContent, "Storage backend down"); // api.error() unwraps detail
  assert.equal(calls.filter(([m, h]) => m === "GET" && h === "/documents?limit=100").length, 1); // no reload after a failed upload
  assert.equal(fileInput.value, ""); // finally still clears the picker
});

test("a change event with nothing picked sends nothing", async () => {
  const { host, calls } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  docsCard(host).querySelector(".card-head-actions input").dispatchEvent(new Event("change")); // picker cancelled: files still empty
  await flush();
  assert.equal(calls.filter(([m]) => m === "POST").length, 0);
});

test("confirming the delete toasts success and reloads library and stats", async () => {
  const { host, calls } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  docsCard(host).querySelectorAll("button.icon-btn")[0].click(); // first row: doc id 1
  const [cancel, confirm] = [...document.querySelectorAll(".dialog-foot button")];
  assert.equal(confirm.textContent, "Delete");
  confirm.click();
  await waitFor(() => document.querySelector('.toast[data-tone="success"]'), "the success toast");
  assert.equal(document.querySelector('.toast[data-tone="success"]').textContent, "Document deleted");
  assert.deepEqual(calls.filter(([m]) => m === "DELETE"), [["DELETE", "/documents/1"]]);
  assert.equal(calls.filter(([m, h]) => m === "GET" && h === "/documents?limit=100").length, 2); // reload after delete
  assert.equal(docsCard(host).querySelectorAll(".rows .row").length, 2); // fixture unchanged: both rows back
});

test("a failed chat removes the typing bubble and shows a danger banner", async () => {
  const { host, chats } = renderKnowledge({ chatResponses: [json({ detail: "Model overloaded" }, 503)] });
  await waitFor(() => settled(host), "panels to settle");
  const form = chatPanelEl(host).querySelector("form.chat-form");
  form.querySelector("textarea.textarea").value = "What is our refund policy?";
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => host.querySelector(".chat-log .banner[data-tone=danger]"), "the error banner");
  assert.equal(host.querySelector(".chat-log .banner[data-tone=danger]").textContent, "Model overloaded"); // api.error() unwraps detail
  const msgs = [...host.querySelectorAll(".chat-log .msg")];
  assert.equal(msgs.length, 1); // the user message only; the pending bubble is gone
  assert.deepEqual(chats[0], { message: "What is our refund policy?", use_rag: true });
});

test("unchecking Use documents switches the chat to plain answers", async () => {
  const { host, chats } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  const box = document.querySelector("#use-rag");
  assert.equal(box.checked, true); // on by default
  box.checked = false;
  const form = chatPanelEl(host).querySelector("form.chat-form");
  form.querySelector("textarea.textarea").value = "Summarise the notes";
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => chats.length === 1, "the chat request");
  assert.deepEqual(chats[0], { message: "Summarise the notes", use_rag: false });
});

test("New chat clears the server history and resets the log", async () => {
  const { host, calls } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  // one exchange first so there is a conversation to clear
  const form = chatPanelEl(host).querySelector("form.chat-form");
  form.querySelector("textarea.textarea").value = "Hello?";
  form.dispatchEvent(new Event("submit"));
  // wait for the final assistant message, not just user + typing bubble: conversationId is only set once the answer lands
  await waitFor(() => host.querySelector(".chat-log .msg[data-role=assistant] .msg-text")?.textContent.includes("Our policy"), "the first exchange");
  [...chatPanelEl(host).querySelectorAll("button")].find((b) => b.textContent === "New chat").click();
  await waitFor(() => document.querySelector('.toast[data-tone="info"]'), "the info toast");
  assert.equal(document.querySelector('.toast[data-tone="info"]').textContent, "Started a new conversation");
  assert.deepEqual(calls.filter(([m]) => m === "DELETE"), [["DELETE", "/conversations/21/history"]]); // the id from the last answer
  assert.equal(host.querySelectorAll(".chat-log .msg").length, 0);
  assert.equal(host.querySelector(".chat-log .empty h3").textContent, "Ask your knowledge base");
});

test("New chat on a fresh panel resets without any request", async () => {
  const { host, calls } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  [...chatPanelEl(host).querySelectorAll("button")].find((b) => b.textContent === "New chat").click();
  await waitFor(() => document.querySelector('.toast[data-tone="info"]'), "the info toast");
  assert.equal(calls.filter(([m]) => m === "DELETE").length, 0); // no conversation id yet: nothing to clear
  assert.equal(host.querySelector(".chat-log .empty h3").textContent, "Ask your knowledge base");
});

test("New chat still resets when the server already lost the history", async () => {
  const { host, calls } = renderKnowledge({ clearResponse: json({ detail: "Conversation not found" }, 404) });
  await waitFor(() => settled(host), "panels to settle");
  const form = chatPanelEl(host).querySelector("form.chat-form");
  form.querySelector("textarea.textarea").value = "Hello?";
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => host.querySelector(".chat-log .msg[data-role=assistant] .msg-text")?.textContent.includes("Our policy"), "the first exchange");
  [...chatPanelEl(host).querySelectorAll("button")].find((b) => b.textContent === "New chat").click();
  await waitFor(() => document.querySelector('.toast[data-tone="info"]'), "the info toast");
  assert.deepEqual(calls.filter(([m]) => m === "DELETE"), [["DELETE", "/conversations/21/history"]]); // the attempt happened
  assert.equal(document.querySelector(".toast[data-tone=danger]"), null); // the error is swallowed, not toasted
  assert.equal(host.querySelectorAll(".chat-log .msg").length, 0);
});

test("submitting a blank message sends nothing", async () => {
  const { host, chats } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  const form = chatPanelEl(host).querySelector("form.chat-form");
  form.querySelector("textarea.textarea").value = "   "; // whitespace only
  form.dispatchEvent(new Event("submit"));
  await flush();
  assert.equal(chats.length, 0);
  assert.equal(host.querySelectorAll(".chat-log .msg").length, 0);
});

test("Enter submits the chat while Shift+Enter and other keys do not", async () => {
  const { host, chats } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  const form = chatPanelEl(host).querySelector("form.chat-form");
  const input = form.querySelector("textarea.textarea");
  input.value = "Quick question";

  const enter = new Event("keydown");
  enter.key = "Enter";
  input.dispatchEvent(enter);
  assert.equal(enter.defaultPrevented, true); // the view stops the browser newline
  await waitFor(() => chats.length === 1, "the Enter submit");
  assert.deepEqual(chats[0], { message: "Quick question", use_rag: true });

  const shiftEnter = new Event("keydown");
  shiftEnter.key = "Enter";
  shiftEnter.shiftKey = true;
  input.dispatchEvent(shiftEnter);
  await flush();
  assert.equal(shiftEnter.defaultPrevented, false); // a newline is allowed
  assert.equal(chats.length, 1);

  const letter = new Event("keydown");
  letter.key = "a";
  input.dispatchEvent(letter);
  await flush();
  assert.equal(chats.length, 1);
});

test("typing resizes the textarea to its content height", async () => {
  const { host } = renderKnowledge();
  await waitFor(() => settled(host), "panels to settle");
  const input = chatPanelEl(host).querySelector("form.chat-form textarea.textarea");
  assert.equal(input.style.height, undefined); // starts unstyled
  input.dispatchEvent(new Event("input"));
  assert.equal(input.style.height, "0px"); // autosize ran: height set from scrollHeight (a constant in the fake)
});

test("citation chips fall back to a bare source when fields are missing", async () => {
  const answer = {
    conversation_id: 31,
    answer: "See the notes.",
    citations: [
      { document: null, chunk: null, excerpt: "" }, // nothing but the index
      { document: "Notes.md", chunk: 0 }, // zero is a real chunk number and must render
    ],
  };
  const { host } = renderKnowledge({ chatResponses: [answer] });
  await waitFor(() => settled(host), "panels to settle");
  const form = chatPanelEl(host).querySelector("form.chat-form");
  form.querySelector("textarea.textarea").value = "Where is this written?";
  form.dispatchEvent(new Event("submit"));
  await waitFor(() => host.querySelectorAll(".chat-log .msg-cites .chip").length === 2, "the citation chips");
  const chips = [...host.querySelectorAll(".chat-log .msg-cites .chip")];
  assert.equal(chips[0].textContent, "[1] source"); // no document name, no chunk number
  assert.equal(chips[0].getAttribute("title"), ""); // no excerpt to hover over
  assert.equal(chips[1].textContent, "[2] Notes.md #0");
});