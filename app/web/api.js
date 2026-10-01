// SPARTON API client. Single place that knows about HTTP, auth and errors.

const TOKEN_KEY = "sparton.token";

export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
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
    let message = `Request failed (${response.status})`;
    let detail;
    try {
      const payload = await response.json();
      detail = payload.detail;
      if (typeof detail === "string") message = detail;
      else if (Array.isArray(detail)) message = detail.map((d) => d.msg).join(", ");
      else if (detail?.message) message = detail.message; // plan limits: { message, limit, plan }
    } catch { /* non-JSON error body — keep the generic message */ }
    throw new ApiError(message, response.status, detail);
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
  verifyEmail: (token) => post("/auth/verify-email", { token }),
  resendVerification: () => post("/auth/resend-verification"),
  forgotPassword: (email) => post("/auth/forgot-password", { email }),
  resetPassword: (token, password) => post("/auth/reset-password", { token, password }),
  changePassword: (currentPassword, newPassword) =>
    post("/auth/change-password", {
      current_password: currentPassword,
      new_password: newPassword,
    }),

  // --- system
  health: () => get("/health"),
  tools: () => get("/tools"),
  metrics: () => request("/metrics"),

  // --- product: the intelligence loop
  overview: () => get("/overview"),

  shops: () => get("/shops"),
  shop: (id) => get(`/shops/${id}`),
  createShop: (payload) => post("/shops", payload),
  updateShop: (id, payload) => patch(`/shops/${id}`, payload),
  deleteShop: (id) => del(`/shops/${id}`),
  crawlShop: (id) => post(`/shops/${id}/crawl`),
  discoverCompetitors: (id) => post(`/shops/${id}/discover`),
  shopReports: (id) => get(`/shops/${id}/reports`),
  generateReport: (id, days = 7) => post(`/shops/${id}/report?days=${days}`),

  competitors: (shopId) =>
    get(`/competitors${shopId ? `?shop_id=${shopId}` : ""}`),
  createCompetitor: (payload) => post("/competitors", payload),
  deleteCompetitor: (id) => del(`/competitors/${id}`),
  crawlCompetitor: (id) => post(`/competitors/${id}/crawl`),

  changes: ({ shopId, kind, severity, unacknowledgedOnly, days = 30, limit = 50 } = {}) => {
    const q = new URLSearchParams({ days: String(days), limit: String(limit) });
    if (shopId) q.set("shop_id", String(shopId));
    if (kind) q.set("kind", kind);
    if (severity) q.set("severity", severity);
    if (unacknowledgedOnly) q.set("unacknowledged_only", "true");
    return get(`/changes?${q}`);
  },
  acknowledgeChange: (id) => post(`/changes/${id}/ack`),
  changeHistory: (id) => get(`/changes/${encodeURIComponent(id)}/history`),

  reports: (limit = 20) => get(`/reports?limit=${limit}`),
  report: (id) => get(`/reports/${id}`),

  // --- billing
  // `plans` is public: the landing page prices itself from it before signup.
  plans: () => get("/billing/plans"),
  plan: () => get("/billing/plan"),
  checkout: (plan) => post("/billing/checkout", { plan }),
  portal: () => post("/billing/portal"),
  usage: (days = 30) => get(`/billing/usage?days=${days}`),

  // --- agent
  runs: (limit = 20) => get(`/agent/runs?limit=${limit}`),
  run: (id) => get(`/agent/runs/${id}`),
  startRun: (message, session_id = "dashboard") => post("/agent/run", { message, session_id }),
  approvals: (statusFilter) =>
    get(`/approvals${statusFilter ? `?status_filter=${statusFilter}` : ""}`),
  resolveApproval: (id, approved, note) => post(`/approvals/${id}/resolve`, { approved, note }),

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
