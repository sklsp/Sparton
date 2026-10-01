// Onboarding: from a shop URL to the first check in three short steps.
//   1. your shop   2. who you compete with   3. the first check, with honest progress
// Progress is read from the competitors themselves (last status, products found), never faked.

import { api } from "../api.js";
import { h, fill, button } from "../ui.js";
import { t, fmtNumber } from "../i18n.js";
import { flapWord } from "../board.js";

const POLL_MS = 3000;

export default function startView(host, { navigate }) {
  let shop = null;
  let timer = null;

  const steps = ["shop", "rivals", "check"];
  const rail = h("ol.ob-rail", steps.map((id, i) =>
    h("li.ob-stop", { "data-step": id }, h("span.ob-n", String(i + 1)), h("span", t(`ob.rail.${id}`)))));
  const body = h("div.ob-body");
  fill(host, h("div.ob", h("h1.view-title", t("ob.title")), rail, body));

  const mark = (id) => {
    for (const li of rail.children) {
      const idx = steps.indexOf(li.dataset.step);
      const cur = steps.indexOf(id);
      li.toggleAttribute("data-done", idx < cur);
      if (idx === cur) li.setAttribute("aria-current", "step"); else li.removeAttribute("aria-current");
    }
  };

  const errorLine = () => h("p.form-error", { role: "alert", hidden: true });
  const showError = (el, err) => { el.textContent = err.status === 402 ? planLimit(err) : err.message; el.hidden = false; };
  const planLimit = (err) => err.detail?.message || t("ob.err.limit");

  /* ---------------------------------------------------------- 1. your shop */
  function stepShop() {
    mark("shop");
    const url = h("input.input#ob-url", { type: "text", inputmode: "url", required: true, autocomplete: "url", spellcheck: false, autocapitalize: "off", placeholder: "jouwwinkel.nl", "aria-describedby": "ob-url-hint" });
    const err = errorLine();
    const submit = button(t("ob.shop.next"), { variant: "primary", type: "submit", size: "lg" });
    fill(body, h("form.ob-plate", {
      onsubmit: async (e) => {
        e.preventDefault();
        err.hidden = true;
        submit.dataset.loading = "true";
        try {
          shop = await api.createShop({ url: normalise(url.value) });
          stepRivals();
        } catch (ex) { showError(err, ex); } finally { delete submit.dataset.loading; }
      },
    },
    h("h2", t("ob.shop.title")),
    h("p.ob-lede", t("ob.shop.lede")),
    h("div.field", h("label", { for: "ob-url" }, t("ob.shop.label")), url, h("p.field-hint#ob-url-hint", t("ob.shop.hint"))),
    err, submit));
    url.focus();
  }

  /* ------------------------------------------------- 2. who you compete with */
  async function stepRivals() {
    mark("rivals");
    const list = h("ul.ob-suggestions", { "aria-busy": "true" }, h("li.ob-searching", t("ob.rivals.searching")));
    const manual = h("input.input#ob-rival", { type: "text", inputmode: "url", spellcheck: false, autocapitalize: "off", placeholder: "concurrent.nl" });
    const err = errorLine();
    const chosen = new Map(); // url -> name
    const created = new Set();
    const count = h("span.ob-count");
    const go = button(t("ob.rivals.go"), { variant: "primary", size: "lg", type: "button", disabled: true });

    const refresh = () => {
      count.textContent = t("ob.rivals.count", { n: chosen.size });
      go.disabled = chosen.size === 0;
    };
    const addRow = (url, label, checked, reason) => {
      const id = `rv-${list.children.length}-${Math.random().toString(36).slice(2, 7)}`;
      const box = h("input", { type: "checkbox", id, checked, onchange: () => { if (box.checked) chosen.set(url, label); else chosen.delete(url); refresh(); } });
      if (checked) chosen.set(url, label);
      list.append(h("li.ob-suggestion", box, h("label", { for: id }, h("strong", label), reason ? h("span.ob-reason", reason) : null)));
      refresh();
    };

    fill(body, h("div.ob-plate",
      h("h2", t("ob.rivals.title")),
      h("p.ob-lede", t("ob.rivals.lede")),
      list,
      h("form.ob-manual", {
        onsubmit: (e) => {
          e.preventDefault();
          const v = normalise(manual.value);
          if (!manual.value.trim()) return;
          if (list.querySelector(".ob-searching, .ob-none")) list.replaceChildren();
          addRow(v, hostOf(v), true, t("ob.rivals.yours"));
          manual.value = "";
          manual.focus();
        },
      }, h("label", { for: "ob-rival" }, t("ob.rivals.add")), h("div.ob-manual-row", manual, button(t("ob.rivals.addBtn"), { type: "submit" }))),
      err,
      h("div.ob-actions", count, go)));

    go.addEventListener("click", async () => {
      err.hidden = true;
      go.dataset.loading = "true";
      try {
        // A retry after a refused URL must not add the ones that already went through twice.
        for (const [url, name] of chosen) {
          if (created.has(url)) continue;
          await api.createCompetitor({ url, name, shop_id: shop.id });
          created.add(url);
        }
        await api.crawlShop(shop.id);
        stepCheck();
      } catch (ex) { showError(err, ex); } finally { delete go.dataset.loading; }
    });

    try {
      const { suggestions = [] } = await api.discoverCompetitors(shop.id);
      list.removeAttribute("aria-busy");
      if (list.querySelector(".ob-searching")) list.replaceChildren();
      for (const s of suggestions) addRow(s.url, s.domain, false, t("ob.rivals.suggested"));
      if (!suggestions.length && !list.children.length) list.append(h("li.ob-none", t("ob.rivals.none")));
    } catch {
      list.removeAttribute("aria-busy");
      if (list.querySelector(".ob-searching")) fill(list, h("li.ob-none", t("ob.rivals.none")));
    }
  }

  /* -------------------------------------------------------- 3. the first check */
  function stepCheck() {
    mark("check");
    const rows = h("ol.board-rows.ob-board-rows");
    const summary = h("p.ob-lede", { role: "status", "aria-live": "polite" }, t("ob.check.queued"));
    const done = h("a.btn", { href: "#/overview", "data-variant": "primary", "data-size": "lg" }, t("ob.check.toDashboard"));
    fill(body, h("div.ob-plate",
      h("h2", t("ob.check.title")),
      h("p.ob-lede", t("ob.check.lede")),
      h("div.board.ob-board", h("div.board-head", h("span.board-title", t("ob.check.board"))), rows),
      summary,
      h("p.ob-fine", t("ob.check.leave")),
      done));

    const render = (competitors) => {
      const states = competitors.map(state);
      rows.replaceChildren(...competitors.map((c, i) => h("li.ob-row", { "data-state": states[i].key },
        h("span.ob-row-name", c.name || c.domain),
        flapWord(states[i].word, { className: "ob-row-flaps" }),
        h("span.ob-row-detail", states[i].detail))));
      const finished = states.filter((s) => s.final).length;
      summary.textContent = finished === competitors.length
        ? t("ob.check.allDone")
        : t("ob.check.progress", { done: finished, n: competitors.length });
      return finished === competitors.length;
    };

    const poll = async () => {
      try {
        const res = await api.competitors(shop.id);
        const list = res.competitors || res;
        if (render(list)) return;
      } catch { /* a missed poll is not an error worth showing; the next one will tell */ }
      timer = setTimeout(poll, POLL_MS);
    };
    poll();
  }

  stepShop();
  return () => clearTimeout(timer);
}

/** What a competitor's first check looks like right now, in board words. */
function state(c) {
  const status = String(c.last_status || "").toUpperCase();
  if (!c.last_crawled_at && !status) return { key: "waiting", word: t("ob.state.waiting"), detail: t("ob.state.waitingDetail") };
  if (["RUNNING", "CRAWLING", "IN_PROGRESS", "QUEUED"].includes(status)) return { key: "reading", word: t("ob.state.reading"), detail: t("ob.state.readingDetail") };
  if (["FAILED", "BLOCKED"].includes(status)) return { key: "failed", word: t("ob.state.failed"), detail: t(status === "BLOCKED" ? "ob.state.blockedDetail" : "ob.state.failedDetail"), final: true };
  const exact = c.data_source === "feed" || c.data_source === "jsonld";
  return {
    key: exact ? "exact" : "extracted",
    word: t("ob.state.done"),
    detail: t(exact ? "ob.state.exactDetail" : "ob.state.extractedDetail", { n: fmtNumber(c.product_count || 0) }),
    final: true,
  };
}

function normalise(value) {
  const v = value.trim();
  return /^https?:\/\//i.test(v) ? v : `https://${v}`;
}
function hostOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, ""); } catch { return url; }
}

