// Dutch and English. One dictionary per language in ./i18n/<lang>.json, loaded once at import.
// The choice is remembered in localStorage; without one, Dutch when the browser says so.
// Switching reloads the page: every view re-renders in the new language with no re-render plumbing.

const KEY = "sparton.lang";
export const LANGS = ["nl", "en"];

function pick() {
  try {
    const saved = localStorage.getItem(KEY);
    if (LANGS.includes(saved)) return saved;
  } catch { /* storage blocked: fall through to the browser language */ }
  return (navigator.language || "").toLowerCase().startsWith("nl") ? "nl" : "en";
}

export const lang = pick();
document.documentElement.lang = lang;

const dict = await fetch(new URL(`./i18n/${lang}.json`, import.meta.url))
  .then((r) => (r.ok ? r.json() : {}))
  .catch(() => ({}));

/** t("nav.overview") or t("plan.shops", { n: 3 }). Missing keys show the key, never undefined. */
export function t(key, vars) {
  let s = dict[key] ?? key;
  if (vars) {
    if ("n" in vars && dict[`${key}.one`] && Number(vars.n) === 1) s = dict[`${key}.one`];
    s = s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));
  }
  return s;
}

export function setLang(next) {
  if (!LANGS.includes(next) || next === lang) return;
  try { localStorage.setItem(KEY, next); } catch { /* still switch for this page */ }
  location.reload();
}

/** Static HTML: data-i18n=KEY sets text; data-i18n-attr=ATTR:KEY;ATTR:KEY sets attributes. */
export function translateDom(root = document) {
  for (const el of root.querySelectorAll("[data-i18n]")) el.textContent = t(el.dataset.i18n);
  for (const el of root.querySelectorAll("[data-i18n-html]")) el.innerHTML = t(el.dataset.i18nHtml);
  for (const el of root.querySelectorAll("[data-i18n-attr]")) {
    for (const pair of el.dataset.i18nAttr.split(";")) {
      const [attr, key] = pair.split(":");
      if (attr && key) el.setAttribute(attr.trim(), t(key.trim()));
    }
  }
}

/** The NL/EN toggle used by every page. */
export function langSwitch() {
  const wrap = document.createElement("div");
  wrap.className = "lang-switch";
  wrap.setAttribute("role", "group");
  wrap.setAttribute("aria-label", t("lang.label"));
  for (const code of LANGS) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = code.toUpperCase();
    b.lang = code;
    b.setAttribute("aria-pressed", String(code === lang));
    b.setAttribute("aria-label", t(`lang.${code}`));
    b.addEventListener("click", () => setLang(code));
    wrap.append(b);
  }
  return wrap;
}

const locale = lang === "nl" ? "nl-NL" : "en-IE";
export const fmtNumber = (v, opts) => new Intl.NumberFormat(locale, opts).format(Number(v ?? 0));
export const fmtMoney = (v, currency = "EUR", digits = 2) =>
  (v == null || Number.isNaN(Number(v)) ? "—"
    : new Intl.NumberFormat(locale, { style: "currency", currency: (currency || "EUR").toUpperCase(), minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(v)));
export const fmtDate = (d, opts = { day: "numeric", month: "short" }) => new Intl.DateTimeFormat(locale, opts).format(d);
export const fmtRelative = (value, unit) => new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(value, unit);
