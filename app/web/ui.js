// Rendering primitives. No framework: a tiny `h()` plus the handful of
// stateful widgets (toasts, dialogs) the dashboard actually needs.

import { t } from "./i18n.js";

/* ------------------------------------------------------------- elements */

/**
 * h("div.card", { onclick }, child, child)  ->  HTMLElement
 * Tag syntax supports `tag.class.class` and `tag#id`.
 */
export function h(spec, props, ...children) {
  const [, tag = "div", rest = ""] = /^([a-z0-9-]*)(.*)$/i.exec(spec) || [];
  const el = document.createElement(tag || "div");
  for (const token of rest.match(/[.#][^.#]+/g) || []) {
    if (token[0] === ".") el.classList.add(token.slice(1));
    else el.id = token.slice(1);
  }
  if (props && (props.nodeType || typeof props !== "object" || Array.isArray(props))) {
    children.unshift(props);
  } else if (props) {
    for (const [key, value] of Object.entries(props)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className += ` ${value}`;
      else if (key === "html") el.innerHTML = value;
      else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
      else if (key.startsWith("on") && typeof value === "function") {
        el.addEventListener(key.slice(2), value);
      } else if (key in el && key !== "list" && typeof value !== "boolean") {
        el[key] = value;
      } else {
        el.setAttribute(key, value === true ? "" : value);
      }
    }
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    if (Array.isArray(child)) append(el, child);
    else el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
}

/** Replace an element's children in one shot. */
export function fill(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

/* ---------------------------------------------------------------- icons */
// One coherent icon language: 20x20, 1.6 stroke, round caps. Inline so the
// dashboard has zero network dependencies.
const PATHS = {
  overview: "M3 10.5 12 3l9 7.5M5.5 9.5V20h13V9.5",
  knowledge: "M4 5.5A1.5 1.5 0 0 1 5.5 4H19v16H5.5A1.5 1.5 0 0 1 4 18.5zM8 4v16",
  intelligence: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14M16 16l4.5 4.5",
  catalog: "M3.5 7.5 12 3l8.5 4.5v9L12 21l-8.5-4.5zM3.5 7.5 12 12m0 0 8.5-4.5M12 12v9",
  agent: "M8 3h8a2 2 0 0 1 2 2v3H6V5a2 2 0 0 1 2-2M4 8h16v9a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2zM9.5 13h.01M14.5 13h.01",
  create: "m12 3 2.2 5.6L20 10l-5 3.6L16.2 20 12 16.8 7.8 20 9 13.6 4 10l5.8-1.4z",
  system: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.2a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-3-1.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9h-.2a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.3-3l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 2.9-1.2V2a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 3 1.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9h.2a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.6 1z",
  approvals: "M9 12.5 11 14.5 15.5 10M12 3l7 3v6c0 4.2-2.9 7.6-7 9-4.1-1.4-7-4.8-7-9V6z",
  refresh: "M20 12a8 8 0 1 1-2.6-5.9M20 4v5h-5",
  search: "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14M16 16l4.5 4.5",
  chevron: "m9 6 6 6-6 6",
  chevronDown: "m6 9 6 6 6-6",
  close: "M6 6l12 12M18 6 6 18",
  menu: "M4 7h16M4 12h16M4 17h16",
  plus: "M12 5v14M5 12h14",
  check: "m5 13 4.5 4.5L19 7",
  alert: "M12 8v5M12 16.5h.01M10.3 4l-7 12A2 2 0 0 0 5 19h14a2 2 0 0 0 1.7-3l-7-12a2 2 0 0 0-3.4 0z",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18M12 11v5M12 8h.01",
  send: "M4.5 12 20 4.5 15.5 20l-3.6-6.4z",
  upload: "M12 16V4m0 0L7.5 8.5M12 4l4.5 4.5M4 16v2.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V16",
  trash: "M4.5 7h15M9.5 7V5h5v2M6.5 7l1 13h9l1-13M10.5 11v5M13.5 11v5",
  doc: "M7 3h7l4 4v14H7zM14 3v4h4",
  moon: "M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5",
  sun: "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4",
  logout: "M9 20H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h3M15.5 16l4.5-4-4.5-4M20 12H9",
  image: "M4 5.5A1.5 1.5 0 0 1 5.5 4h13A1.5 1.5 0 0 1 20 5.5v13a1.5 1.5 0 0 1-1.5 1.5h-13A1.5 1.5 0 0 1 4 18.5zM8.5 10a1.2 1.2 0 1 0 0-2.4 1.2 1.2 0 0 0 0 2.4M4.5 16.5 9 12l5 5m1.5-2 2 2",
  cpu: "M8 8h8v8H8zM9 4v4m6-4v4M9 16v4m6-4v4M4 9h4m-4 6h4m8-6h4m-4 6h4",
  bolt: "M13 3 5 13.5h6L11 21l8-10.5h-6z",
  store: "M4 9h16v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1zM3.5 9 5 4h14l1.5 5a2.5 2.5 0 0 1-4.2 1.9A2.5 2.5 0 0 1 12 10.9a2.5 2.5 0 0 1-4.3 0A2.5 2.5 0 0 1 3.5 9",
  target: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18M12 17a5 5 0 1 0 0-10 5 5 0 0 0 0 10M12 13a1 1 0 1 0 0-2 1 1 0 0 0 0 2",
  clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18M12 7v5.5l3.5 2",
  users: "M16 20v-1.5a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4V20M9.5 10.5a3.2 3.2 0 1 0 0-6.5 3.2 3.2 0 0 0 0 6.5M21 20v-1.5a4 4 0 0 0-3-3.9M16 4.1a4 4 0 0 1 0 7.4",
  chat: "M20 12.5a7.5 7.5 0 0 1-10.9 6.7L4 20.5l1.4-4.8A7.5 7.5 0 1 1 20 12.5",
  chart: "M4 20V9m5 11V4m5 16v-7m5 7V7",
  db: "M12 8c4.4 0 8-1.1 8-2.5S16.4 3 12 3 4 4.1 4 5.5 7.6 8 12 8M4 5.5v13C4 19.9 7.6 21 12 21s8-1.1 8-2.5v-13M4 12c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5",
};

export function icon(name, size = 18) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  el.setAttribute("viewBox", "0 0 24 24");
  el.setAttribute("width", size);
  el.setAttribute("height", size);
  el.setAttribute("fill", "none");
  el.setAttribute("stroke", "currentColor");
  el.setAttribute("stroke-width", "1.6");
  el.setAttribute("stroke-linecap", "round");
  el.setAttribute("stroke-linejoin", "round");
  el.setAttribute("aria-hidden", "true");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", PATHS[name] || PATHS.info);
  el.append(path);
  return el;
}

/* ----------------------------------------------------------- formatting */
const nf = new Intl.NumberFormat("en-US");
const cf = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });

export const num = (value) => nf.format(Number(value ?? 0));
export const money = (value) => cf.format(Number(value ?? 0));

export function bytes(value) {
  const n = Number(value ?? 0);
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

/** "3m ago" — tolerant of the API's `str(datetime)` output and of nulls. */
export function ago(value) {
  if (!value) return "—";
  const raw = String(value).replace(" ", "T");
  const date = new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(raw) ? raw : `${raw}Z`);
  if (Number.isNaN(date.getTime())) return "—";
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 45) return "just now";
  const steps = [[60, "m", 60], [3600, "h", 3600], [86400, "d", 86400]];
  for (const [limit, unit, div] of steps) {
    if (seconds < limit * 60 || unit === "d") {
      if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
      if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
      const days = Math.round(seconds / 86400);
      return days > 30 ? date.toLocaleDateString() : `${days}d ago`;
    }
  }
  return date.toLocaleDateString();
}

export function when(value) {
  if (!value) return "—";
  const raw = String(value).replace(" ", "T");
  const date = new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(raw) ? raw : `${raw}Z`);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

/** Map a backend status string onto a semantic colour. */
export function tone(status) {
  const s = String(status || "").toUpperCase();
  if (["COMPLETED", "SUCCEEDED", "SUCCESS", "APPROVED", "ACTIVE", "DONE", "OK", "READY"].includes(s)) return "success";
  if (["FAILED", "ERROR", "REJECTED", "CANCELLED", "CANCELED", "DEGRADED"].includes(s)) return "danger";
  if (["PENDING", "QUEUED", "AWAITING_APPROVAL", "PAUSED", "WAITING"].includes(s)) return "warning";
  if (["RUNNING", "IN_PROGRESS", "CRAWLING", "PROCESSING"].includes(s)) return "info";
  return "neutral";
}

export const title = (value) =>
  String(value ?? "").replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

/* ---------------------------------------------------------- ui fragments */
export const badge = (text, statusTone) =>
  h("span.badge", { "data-tone": statusTone || tone(text) }, title(text));

export const statusBadge = (status, live = false) =>
  h("span.badge", { "data-tone": tone(status) },
    live ? h("span.dot.dot-live") : null,
    title(status));

export function card(headingOrOptions, ...body) {
  const opts = typeof headingOrOptions === "string" ? { title: headingOrOptions } : headingOrOptions;
  const el = h("section.card");
  if (opts.title) {
    el.append(h("header.card-head",
      h("div", h("h2", opts.title), opts.sub ? h("div.sub", opts.sub) : null),
      opts.actions ? h("div.card-head-actions", opts.actions) : null));
  }
  el.append(h(opts.flush ? "div.card-body.flush" : "div.card-body", body));
  if (opts.foot) el.append(h("footer.card-foot", opts.foot));
  return el;
}

export const metric = ({ label, value, sub, iconName, tone: t }) =>
  h("article.card.metric",
    h("div.metric-top",
      h("span.metric-label", label),
      iconName ? h("span.metric-icon", icon(iconName, 16)) : null),
    h("div.metric-value", { style: t ? { color: `var(--${t})` } : null }, value),
    sub ? h("div.metric-sub", sub) : null);

export const button = (label, { variant, size, iconName, onClick, disabled, type = "button", ...rest } = {}) =>
  h("button.btn", {
    type,
    "data-variant": variant,
    "data-size": size,
    disabled,
    onclick: onClick,
    ...rest,
  }, iconName ? icon(iconName, size === "sm" ? 14 : 16) : null, label);

export const skeleton = (lines = 4) =>
  h("div.sk-stack", { "aria-busy": "true", "aria-label": t("ui.loading") },
    Array.from({ length: lines }, (_, i) =>
      h("div.skeleton.sk-line", { style: { width: `${100 - (i % 3) * 18}%` } })));

export const skeletonMetrics = (count = 4) =>
  h("div.grid.grid-metrics", { "aria-busy": "true" },
    Array.from({ length: count }, () =>
      h("article.card.metric",
        h("div.skeleton.sk-line", { style: { width: "50%" } }),
        h("div.skeleton", { style: { width: "70%", height: "28px", margin: "4px 0" } }),
        h("div.skeleton.sk-line", { style: { width: "40%" } }))));

export const empty = ({ iconName = "info", title: heading, message, action }) =>
  h("div.empty",
    h("div.empty-icon", icon(iconName, 20)),
    h("h3", heading),
    message ? h("p", message) : null,
    // Views pass either a ready element or { label, onClick }.
    action?.nodeType ? action : action ? button(action.label, { variant: "primary", onClick: action.onClick }) : null);

export const errorState = ({ title: heading = t("ui.error"), message, onRetry }) =>
  h("div.error-state", { role: "alert" },
    h("div.empty-icon", icon("alert", 20)),
    h("h3", heading),
    message ? h("p", message) : null,
    onRetry ? button(t("ui.retry"), { iconName: "refresh", size: "sm", onClick: onRetry }) : null);

export const banner = (message, { tone: t = "info", action } = {}) =>
  h("div.banner", { "data-tone": t, role: t === "danger" ? "alert" : "status" },
    icon(t === "danger" ? "alert" : t === "warning" ? "alert" : "info", 16),
    h("div", message),
    action ? h("div.banner-actions", action) : null);

export const field = (label, control) => h("label.field", h("span", label), control);

export const meter = (fraction, t) =>
  h("div.meter", { "data-tone": t, role: "presentation" },
    h("span", { style: { width: `${Math.max(0, Math.min(1, fraction || 0)) * 100}%` } }));

/** Scroll behaviour that honours the reduced-motion preference. */
export const scrollBehavior = () =>
  (matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth");

/* -------------------------------------------------------------- toasts */
let toastHost;

export function toast(message, t = "info") {
  toastHost ||= document.body.appendChild(h("div.toasts", { "aria-live": "polite" }));
  const el = h("div.toast", { "data-tone": t, role: "status" },
    h("span.toast-icon", icon(t === "success" ? "check" : t === "danger" ? "alert" : "info", 16)),
    h("div", message),
    h("button.toast-close", { "aria-label": t("ui.dismiss"), onclick: () => close() }, icon("close", 14)));

  const close = () => {
    if (el.dataset.closing) return;
    el.dataset.closing = "true";
    el.addEventListener("animationend", () => el.remove(), { once: true });
    setTimeout(() => el.remove(), 400);
  };
  toastHost.append(el);
  setTimeout(close, t === "danger" ? 7000 : 4000);
  return close;
}

/* -------------------------------------------------------------- dialog */
/** Native <dialog>: focus trapping, Esc and the backdrop come for free. */
export function dialog({ title: heading, body, actions, onClose }) {
  const el = h("dialog", { "aria-labelledby": "dlg-title" },
    h("header.dialog-head",
      h("h2#dlg-title", heading),
      h("button.icon-btn", { "aria-label": t("ui.close"), type: "button", onclick: () => el.close() }, icon("close", 16))),
    h("div.dialog-body", body),
    actions ? h("footer.dialog-foot", actions) : null);

  el.addEventListener("close", () => { el.remove(); onClose?.(el.returnValue); });
  document.body.append(el);
  el.showModal();
  return el;
}

export function confirmDialog({ title: heading, message, confirmLabel = t("ui.confirm"), variant = "primary" }) {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => { if (!settled) { settled = true; resolve(value); } };
    const el = dialog({
      title: heading,
      body: h("p", { style: { color: "var(--text-2)" } }, message),
      actions: [
        button(t("ui.cancel"), { variant: "ghost", onClick: () => { finish(false); el.close(); } }),
        button(confirmLabel, { variant, onClick: () => { finish(true); el.close(); } }),
      ],
      onClose: () => finish(false),
    });
  });
}

/* ----------------------------------------------------- async section glue */
/**
 * Render loading -> data | error into `host`, re-running on `reload()`.
 * Keeps every panel's three states consistent without a state library.
 */
export function asyncPanel(host, load, render, { loading } = {}) {
  let generation = 0;
  const run = async () => {
    const mine = ++generation;
    fill(host, loading ? loading() : skeleton());
    try {
      const data = await load();
      if (mine !== generation) return;
      fill(host, render(data, run));
    } catch (err) {
      if (mine !== generation || err.name === "AbortError") return;
      fill(host, errorState({ message: err.message, onRetry: run }));
    }
  };
  run();
  return run;
}
