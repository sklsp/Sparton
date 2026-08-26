// Knowledge — document library, RAG index health, grounded chat.

import { api } from "../api.js";
import {
  h, fill, icon, card, metric, button, skeletonMetrics, empty, banner, toast,
  confirmDialog, num, bytes, ago, asyncPanel,
} from "../ui.js";

export default function knowledge(view) {
  view.append(
    h("div.page-head",
      h("div",
        h("h1", "Knowledge"),
        h("p", "Documents indexed for retrieval. Chat answers cite the chunks they used."))));

  const statsHost = h("div");
  view.append(statsHost);
  const reloadStats = asyncPanel(statsHost, loadStats, renderStats, { loading: () => skeletonMetrics(3) });

  const grid = h("div.grid.grid-2");
  view.append(grid);

  // --- documents
  const docsCard = h("section.card");
  const docsBody = h("div.card-body.flush");
  const fileInput = h("input", {
    type: "file", multiple: true, hidden: true,
    accept: ".pdf,.txt,.md,.docx,.doc,.csv,.json",
    "aria-label": "Choose documents to upload",
  });
  const uploadBtn = button("Upload", { variant: "primary", size: "sm", iconName: "upload", onClick: () => fileInput.click() });

  fileInput.addEventListener("change", async () => {
    if (!fileInput.files?.length) return;
    uploadBtn.dataset.loading = "true";
    try {
      const result = await api.uploadDocuments(fileInput.files);
      const failures = (result.results || []).filter((r) => r.error);
      if (result.uploaded) toast(`Indexed ${result.uploaded} document${result.uploaded === 1 ? "" : "s"}`, "success");
      for (const failure of failures) toast(`${failure.filename}: ${failure.error}`, "danger");
      reloadDocs();
      reloadStats();
    } catch (err) {
      toast(err.message, "danger");
    } finally {
      delete uploadBtn.dataset.loading;
      fileInput.value = "";
    }
  });

  docsCard.append(
    h("header.card-head",
      h("div", h("h2", "Documents"), h("div.sub", "PDF, Word, Markdown and plain text")),
      h("div.card-head-actions", fileInput, uploadBtn)),
    docsBody);
  grid.append(docsCard);

  const reloadDocs = asyncPanel(docsBody, () => api.documents(100),
    (data, reload) => renderDocuments(data, () => { reload(); reloadStats(); }, () => fileInput.click()));

  // --- chat
  grid.append(chatPanel());

  // --- prompt templates
  const promptsHost = h("section.card");
  const promptsBody = h("div.card-body.flush");
  promptsHost.append(
    h("header.card-head",
      h("div", h("h2", "Prompt templates"), h("div.sub", "Reusable system prompts available to chat and the agent"))),
    promptsBody);
  view.append(promptsHost);
  asyncPanel(promptsBody, () => api.prompts(), renderPrompts);
}

/* ----------------------------------------------------------------- stats */

async function loadStats() {
  const [docs, rag] = await Promise.all([api.documents(200), api.ragStatus().catch(() => null)]);
  return { docs, rag };
}

function renderStats({ docs, rag }) {
  const documents = docs.documents || [];
  const totalBytes = documents.reduce((sum, d) => sum + (d.size_bytes || 0), 0);
  const wrap = h("div");

  if (!rag) {
    wrap.append(banner("The RAG index could not be read. Uploads will still store files, but chat cannot cite them.", { tone: "warning" }));
  }

  wrap.append(h("div.grid.grid-metrics", { style: { marginTop: rag ? "0" : "16px" } },
    metric({ label: "Documents", value: num(docs.count), iconName: "doc", sub: bytes(totalBytes) }),
    metric({ label: "Indexed chunks", value: num(rag?.chunks ?? 0), iconName: "db",
      sub: rag?.documents != null ? `${num(rag.documents)} versions tracked` : "index unavailable" }),
    metric({ label: "Embeddings", value: rag?.embedding_backend ? String(rag.embedding_backend) : "—",
      iconName: "cpu", sub: rag?.embedding_model || "no model reported" })));
  return wrap;
}

/* ------------------------------------------------------------- documents */

function renderDocuments(data, reload, onUpload) {
  const documents = data.documents || [];
  if (!documents.length) {
    return empty({
      iconName: "upload",
      title: "No documents yet",
      message: "Upload files and every chunk becomes searchable context for chat and the agent.",
      action: button("Upload documents", { size: "sm", variant: "primary", iconName: "upload", onClick: onUpload }),
    });
  }

  const search = h("input.input", { type: "search", placeholder: "Filter documents…", "aria-label": "Filter documents" });
  const list = h("div.rows");

  const paint = () => {
    const needle = search.value.trim().toLowerCase();
    const matches = documents.filter((d) => !needle || d.title.toLowerCase().includes(needle));
    if (!matches.length) {
      fill(list, empty({ iconName: "search", title: "No matches", message: `Nothing matches “${search.value}”.` }));
      return;
    }
    fill(list, matches.map((doc) => {
      const remove = async () => {
        const ok = await confirmDialog({
          title: "Delete this document?",
          message: `“${doc.title}” and its ${doc.chunks} indexed chunks will be removed. This cannot be undone.`,
          confirmLabel: "Delete", variant: "danger",
        });
        if (!ok) return;
        try {
          await api.deleteDocument(doc.id);
          toast("Document deleted", "success");
          reload();
        } catch (err) {
          toast(err.message, "danger");
        }
      };
      return h("div.row",
        h("span.action-icon", icon("doc", 15)),
        h("div.row-main",
          h("div.row-title.truncate", { title: doc.title }, doc.title),
          h("div.row-sub", `${bytes(doc.size_bytes)} · ${num(doc.chunks)} chunks · ${ago(doc.created_at)}`)),
        h("div.row-side",
          h("button.icon-btn", { type: "button", "aria-label": `Delete ${doc.title}`, onclick: remove },
            icon("trash", 15))));
    }));
  };

  search.addEventListener("input", paint);
  paint();
  return h("div",
    h("div", { style: { padding: "16px 20px" } }, h("div.search", icon("search", 15), search)),
    list);
}

/* ------------------------------------------------------------------ chat */

function chatPanel() {
  let conversationId = null;
  const log = h("div.chat-log", { role: "log", "aria-live": "polite", "aria-label": "Chat transcript" });
  const input = h("textarea.textarea", {
    placeholder: "Ask about your documents…", rows: 1, "aria-label": "Message", maxlength: 8000,
  });
  const send = button("Send", { variant: "primary", iconName: "send", type: "submit" });
  const useRag = h("input", { type: "checkbox", checked: true, id: "use-rag" });

  const resetLog = () => fill(log, empty({
    iconName: "chat",
    title: "Ask your knowledge base",
    message: "Answers are grounded in your indexed documents and cite the chunks they used.",
  }));
  resetLog();

  const push = (role, content, citations) => {
    log.querySelector(".empty")?.remove();
    const el = h("div.msg", { "data-role": role },
      h("div.msg-avatar", role === "user" ? "you" : "sp"),
      h("div.msg-body",
        h("div.msg-text", content),
        citations?.length
          ? h("div.msg-cites", citations.map((c, i) =>
              h("span.chip", { title: c.excerpt || "" },
                `[${i + 1}] ${c.document || "source"}${c.chunk != null ? ` #${c.chunk}` : ""}`)))
          : null));
    log.append(el);
    log.scrollTop = log.scrollHeight;
    return el;
  };

  const form = h("form.chat-form", {
    onsubmit: async (event) => {
      event.preventDefault();
      const message = input.value.trim();
      if (!message) return;
      push("user", message);
      input.value = "";
      input.style.height = "";
      send.dataset.loading = "true";
      const pending = push("assistant", "");
      fill(pending.querySelector(".msg-text"), h("span.typing", h("i"), h("i"), h("i")));
      try {
        const answer = await api.chat(message, conversationId, useRag.checked);
        conversationId = answer.conversation_id;
        pending.remove();
        push("assistant", answer.answer, answer.citations);
      } catch (err) {
        pending.remove();
        log.append(banner(err.message, { tone: "danger" }));
        log.scrollTop = log.scrollHeight;
      } finally {
        delete send.dataset.loading;
        input.focus();
      }
    },
  },
    input, send);

  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
  });

  const clear = button("New chat", { size: "sm", variant: "ghost", onClick: async () => {
    if (conversationId) { try { await api.clearConversation(conversationId); } catch { /* already gone */ } }
    conversationId = null;
    resetLog();
    toast("Started a new conversation", "info");
  } });

  const panel = h("section.card",
    h("header.card-head",
      h("div", h("h2", "Chat"), h("div.sub", "Retrieval-augmented, with citations")),
      h("div.card-head-actions",
        h("label", { for: "use-rag", style: { display: "flex", alignItems: "center", gap: "6px", font: "var(--t-meta)", color: "var(--text-2)" } },
          useRag, "Use documents"),
        clear)),
    h("div.chat", log, form));
  return panel;
}

/* --------------------------------------------------------------- prompts */

function renderPrompts(data) {
  const prompts = data.prompts || [];
  if (!prompts.length) {
    return empty({
      iconName: "doc",
      title: "No prompt templates",
      message: "Templates created via POST /prompts appear here and can be reused across chat and agent runs.",
    });
  }
  return h("div.rows", prompts.map((prompt) =>
    h("div.row",
      h("div.row-main",
        h("div.row-title", prompt.name),
        h("div.row-sub", prompt.description || "No description")),
      h("div.row-side", h("span.chip", prompt.key)))));
}
