// SPARTON API client. Single place that knows about HTTP, auth and errors.

const TOKEN_KEY = "sparton.token";

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

export const token = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (value) => localStorage.setItem(TOKEN_KEY, value),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

/** Fires when a request comes back 401 so the shell can bounce to login. */
export const onUnauthorized = new Set();

async function request(path, { method = "GET", body, form, signal } = {}) {
  const headers = {};
  const auth = token.get();
  if (auth) headers.Authorization = `Bearer ${auth}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(path, {
      method,
      headers,
      body: form ?? (body === undefined ? undefined : JSON.stringify(body)),
      signal,
    });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError("Could not reach the SPARTON API. Is the server running?", 0);
  }

  if (response.status === 401) {
    token.clear();
    onUnauthorized.forEach((fn) => fn());
    throw new ApiError("Your session expired. Sign in again.", 401);
  }

  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === "string") detail = payload.detail;
      else if (Array.isArray(payload.detail)) detail = payload.detail.map((d) => d.msg).join(", ");
    } catch { /* non-JSON error body — keep the generic message */ }
    throw new ApiError(detail, response.status);
  }

  if (response.status === 204) return null;
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response.text();
}

const get = (path, opts) => request(path, opts);
const post = (path, body, opts) => request(path, { method: "POST", body, ...opts });
const patch = (path, body) => request(path, { method: "PATCH", body });
const put = (path, body) => request(path, { method: "PUT", body });
const del = (path) => request(path, { method: "DELETE" });

export const api = {
  // --- auth
  login: (email, password) => post("/auth/login", { email, password }),
  register: (email, password, organization_name) =>
    post("/auth/register", { email, password, organization_name }),
  logout: () => post("/auth/logout"),
  me: () => get("/auth/me"),

  // --- system
  health: () => get("/health"),
  tools: () => get("/tools"),
  metrics: () => request("/metrics"),

  // --- agent
  runs: (limit = 20) => get(`/agent/runs?limit=${limit}`),
  run: (id) => get(`/agent/runs/${id}`),
  startRun: (message, session_id = "dashboard") => post("/agent/run", { message, session_id }),
  approvals: (statusFilter) =>
    get(`/approvals${statusFilter ? `?status_filter=${statusFilter}` : ""}`),
  resolveApproval: (id, approved, note) => post(`/approvals/${id}/resolve`, { approved, note }),

  // --- knowledge
  documents: (limit = 50) => get(`/documents?limit=${limit}`),
  deleteDocument: (id) => del(`/documents/${id}`),
  uploadDocuments: (files) => {
    const form = new FormData();
    for (const file of files) form.append("files", file);
    return request("/documents/upload", { method: "POST", form });
  },
  ragStatus: () => get("/rag/status"),
  chat: (message, conversation_id, use_rag = true) =>
    post("/chat", { message, conversation_id, use_rag }),
  conversationHistory: (id) => get(`/conversations/${id}/history`),
  clearConversation: (id) => del(`/conversations/${id}/history`),
  prompts: () => get("/prompts"),
  createPrompt: (payload) => post("/prompts", payload),

  // --- intelligence
  researchJobs: (limit = 20) => get(`/intelligence/jobs?limit=${limit}`),
  startResearch: (query, start_urls = []) => post("/intelligence/jobs", { query, start_urls }),
  stores: (limit = 50) => get(`/intelligence/stores?limit=${limit}`),
  opportunities: (kind, limit = 50) =>
    get(`/intelligence/opportunities?limit=${limit}${kind ? `&kind=${kind}` : ""}`),
  opportunity: (id) => get(`/intelligence/opportunities/${id}`),

  // --- catalog
  products: (search, limit = 50) =>
    get(`/products?limit=${limit}${search ? `&search=${encodeURIComponent(search)}` : ""}`),
  product: (id) => get(`/products/${id}`),
  analytics: () => get("/analytics/summary"),

  // --- create
  comfyStatus: () => get("/comfyui/status"),
  workflows: () => get("/comfyui/workflows"),
  validateGeneration: (payload) => post("/comfyui/validate-generation", payload),
  generate: (payload) => post("/comfyui/generate", payload),
  generated: (limit = 50) => get(`/generated?limit=${limit}`),
  datasets: () => get("/datasets"),
  createDataset: (payload) => post("/datasets", payload),
  datasetImages: (id) => get(`/datasets/${id}/images`),
  validateDataset: (id) => get(`/datasets/${id}/validate`),
  trainingStatus: () => get("/training/status"),
  trainingPresets: () => get("/training/presets"),
  hardware: () => get("/training/hardware"),
  trainingProjects: () => get("/training/projects"),
  createTrainingProject: (payload) => post("/training/projects", payload),

  // --- admin
  users: () => get("/admin/users"),
  patchUser: (id, payload) => patch(`/admin/users/${id}`, payload),
};

/**
 * Promise.allSettled for panels that merge several endpoints.
 *
 * Plain allSettled would turn a total outage into an innocent-looking empty
 * state, so a run where *every* source failed rethrows and the panel shows its
 * error state instead. Partial failures still degrade gracefully.
 */
export async function settleAll(promises) {
  const results = await Promise.allSettled(promises);
  if (results.length && results.every((r) => r.status === "rejected")) throw results[0].reason;
  return results;
}

/** Value of a settled result, or `fallback` when it rejected. */
export const settledValue = (result, fallback) =>
  (result.status === "fulfilled" ? result.value : fallback);

export { get, post, patch, put, del };
