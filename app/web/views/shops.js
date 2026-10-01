// Shops and the competitors watched for each. Every competitor row says how sure its data is
// (exact from a feed, or extracted from the page), when it was last read, and what it found.

import { api } from "../api.js";
import { h, fill, toast, button, confirmDialog, errorState } from "../ui.js";
import { t, fmtNumber, fmtDate, fmtRelative } from "../i18n.js";
import { loadingBoard } from "./overview.js";
import { platformLabel, isPlanLimit, planLimitMessage } from "./shared.js";

export default function shopsView(host) {
  const run = async () => {
    fill(host, head(), loadingBoard(3));
    try {
      const [{ shops = [] }, comps] = await Promise.all([api.shops(), api.competitors()]);
      const competitors = comps.competitors || comps;
      if (!shops.length) {
        fill(host, head(), h("section.plate.empty-plate",
          h("h2", t("sh.empty.title")),
          h("p", t("sh.empty.body")),
          h("a.btn", { href: "#/start", "data-variant": "primary", "data-size": "lg" }, t("sh.empty.cta"))));
        return;
      }
      fill(host, head(true), shops.map((shop) => shopSection(shop, competitors.filter((c) => c.shop_id === shop.id), run)));
    } catch (err) {
      fill(host, head(), errorState({ title: t("sh.error"), message: err.message, onRetry: run }));
    }
  };
  run();
}

function head(withAdd = false) {
  return h("header.view-head.view-head-row",
    h("div", h("h1.view-title", t("sh.title")), h("p.view-sub", t("sh.sub"))),
    withAdd ? h("a.btn", { href: "#/start" }, t("sh.addShop")) : null);
}

const when = (iso) => {
  if (!iso) return null;
  const days = Math.round((Date.now() - new Date(iso).getTime()) / 864e5);
  return days < 1 ? fmtRelative(-Math.max(1, Math.round((Date.now() - new Date(iso).getTime()) / 36e5)), "hour")
    : days < 14 ? fmtRelative(-days, "day") : fmtDate(new Date(iso));
};

function cadence(hours) {
  if (hours <= 12) return t("plan.freq.twiceDaily");
  if (hours <= 24) return t("plan.freq.daily");
  return t("plan.freq.weekly");
}

function shopSection(shop, competitors, reload) {
  const check = button(t("sh.checkNow"), { size: "sm", variant: "board", onClick: async () => {
    check.dataset.loading = "true";
    try {
      await api.crawlShop(shop.id);
      toast(t("sh.checkQueued"), "success");
      reload();
    } catch (err) { toast(err.message, "danger"); } finally { delete check.dataset.loading; }
  } });
  if (!competitors.length) check.disabled = true;

  return h("section.shop-block", { "aria-labelledby": `shop-${shop.id}` },
    h("header.shop-head",
      h("div",
        h("h2#shop-" + shop.id, shop.name || shop.domain),
        h("p.shop-meta",
          h("a", { href: shop.url, target: "_blank", rel: "noopener noreferrer" }, shop.domain),
          ` · ${platformLabel(shop.platform)} · ${cadence(shop.crawl_frequency_hours)}`,
          shop.next_crawl_at ? ` · ${t("sh.next", { when: fmtDate(new Date(shop.next_crawl_at), { weekday: "short", day: "numeric", month: "short" }) })}` : "")),
      check),
    h("div.plate.comp-plate",
      competitors.length
        ? h("ul.comp-list", competitors.map((c) => competitorRow(c, reload)))
        : h("p.comp-empty", t("sh.noRivals")),
      addCompetitor(shop, reload)));
}

function competitorRow(c, reload) {
  const status = String(c.last_status || "").toUpperCase();
  const failed = status === "FAILED" || status === "BLOCKED";
  const exact = c.data_source === "feed" || c.data_source === "jsonld";
  const never = !c.last_crawled_at;
  const remove = button(t("sh.remove"), { size: "sm", variant: "ghost", onClick: async () => {
    const ok = await confirmDialog({ title: t("sh.removeTitle", { name: c.name || c.domain }), message: t("sh.removeBody"), confirmLabel: t("sh.remove"), variant: "danger" });
    if (!ok) return;
    try { await api.deleteCompetitor(c.id); toast(t("sh.removed"), "success"); reload(); } catch (err) { toast(err.message, "danger"); }
  } });

  return h("li.comp-row", { "data-state": never ? "new" : failed ? "failed" : exact ? "exact" : "extracted" },
    h("span.comp-mark", { "aria-hidden": "true" }),
    h("div.comp-main",
      h("strong.comp-name", c.name || c.domain),
      h("span.comp-meta",
        h("a", { href: c.url, target: "_blank", rel: "noopener noreferrer" }, c.domain),
        ` · ${platformLabel(c.platform)}`)),
    h("div.comp-facts",
      h("span.src-tag", { "data-src": never ? "none" : exact ? "exact" : "extracted" },
        never ? t("src.notYet") : t(exact ? "src.exact" : "src.extracted")),
      h("span.comp-count", never ? t("sh.waitingFirst") : failed
        ? t(status === "BLOCKED" ? "sh.blocked" : "sh.failed")
        : t("sh.products", { n: fmtNumber(c.product_count || 0) }) + (c.last_crawled_at ? ` · ${when(c.last_crawled_at)}` : ""))),
    remove);
}

function addCompetitor(shop, reload) {
  const id = `add-rival-${shop.id}`;
  const input = h("input.input", { id, type: "text", inputmode: "url", autocomplete: "off", spellcheck: false, placeholder: "concurrent.nl", required: true });
  const msg = h("p.form-error", { role: "alert", hidden: true });
  const submit = button(t("sh.add"), { type: "submit" });
  return h("form.comp-add", {
    onsubmit: async (e) => {
      e.preventDefault();
      msg.hidden = true;
      submit.dataset.loading = "true";
      const v = input.value.trim();
      try {
        await api.createCompetitor({ url: /^https?:\/\//i.test(v) ? v : `https://${v}`, shop_id: shop.id });
        toast(t("sh.added"), "success");
        reload();
      } catch (err) {
        msg.textContent = isPlanLimit(err) ? `${planLimitMessage(err)} ${t("sh.upgrade")}` : err.message;
        msg.hidden = false;
      } finally { delete submit.dataset.loading; }
    },
  }, h("label", { for: id }, t("sh.addLabel")), h("div.ob-manual-row", input, submit), msg);
}
