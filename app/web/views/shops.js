// Shops and competitors — the onboarding screen.
//
// This is where a new customer does the one thing that matters: add a shop,
// then confirm the competitors to watch. Both are single forms, because a
// multi-step wizard on a phone is a conversion problem.
//
// Construction order is deliberate: `reload` is declared before the forms,
// because every form's submit handler calls it.

import { api } from "../api.js";
import { h, fill, asyncPanel, empty, toast, ago, badge, button, confirmDialog } from "../ui.js";
import { crawlTone, platformLabel, isPlanLimit, planLimitMessage } from "./shared.js";

/** A form that reports a plan limit as an instruction, not an error. */
function formSubmit({ fields, submitLabel, read, onSubmit, after }) {
  const message = h("p.form-message", { role: "status" });
  message.hidden = true;
  const submit = button(submitLabel, { type: "submit", variant: "primary" });

  const form = h("form.form-grid", {
    onsubmit: async (event) => {
      event.preventDefault();
      message.hidden = true;
      submit.disabled = true;
      submit.dataset.loading = "true";
      try {
        await onSubmit();
        toast(`${submitLabel.replace(/s$/, "")} added`, "success");
        form.reset();
        after?.();
      } catch (error) {
        // A plan limit is not something the user can retry away, so it says
        // what to do rather than only what went wrong.
        const limited = isPlanLimit(error);
        message.dataset.tone = limited ? "warning" : "danger";
        message.textContent = limited
          ? `${planLimitMessage(error)} Change plan in Billing to add more.`
          : error.message;
        message.hidden = false;
      } finally {
        submit.disabled = false;
        delete submit.dataset.loading;
      }
    },
  }, fields, h("div.form-actions", submit));

  return h("div.stack", form, message);
}

const fieldValue = (node, name) =>
  node.querySelector(`[name=${name}]`)?.value.trim() ?? "";

export default function shopsView(host) {
  const listHost = h("div.stack");
  let reload = () => {};

  /* ------------------------------------------------------------ add a shop */
  const shopForm = formSubmit({
    submitLabel: "Add shop",
    fields: [
      h("label.field", h("span", "Shop URL"),
        h("input.input", {
          type: "url", name: "url", required: true,
          placeholder: "your-shop.myshopify.com", autocomplete: "url",
        })),
      h("label.field", h("span", "Name"),
        h("input.input", {
          type: "text", name: "name", placeholder: "Your shop name (optional)",
        })),
      h("label.field", h("span", "Category"),
        h("input.input", {
          type: "text", name: "category", placeholder: "homeware, outdoor, beauty…",
        })),
    ],
    onSubmit: () => api.createShop({
      url: fieldValue(shopForm, "url"),
      name: fieldValue(shopForm, "name"),
      category: fieldValue(shopForm, "category"),
    }),
    after: () => reload(),
  });

  /* ----------------------------------------------------- add a competitor */
  const shopPicker = h("select.input", { "aria-label": "Attach to shop" });
  const competitorForm = formSubmit({
    submitLabel: "Add competitor",
    fields: [
      h("label.field", h("span", "Competitor URL"),
        h("input.input", {
          type: "url", name: "url", required: true,
          placeholder: "competitor-shop.myshopify.com", autocomplete: "off",
        })),
      h("label.field", h("span", "Shop"), shopPicker),
    ],
    onSubmit: () => api.createCompetitor({
      url: fieldValue(competitorForm, "url"),
      shop_id: shopPicker.value ? Number(shopPicker.value) : null,
    }),
    after: () => reload(),
  });

  fill(host,
    h("div.toolbar",
      h("h2.view-title", "Shops"),
      h("div.toolbar-controls",
        button("Refresh", { onClick: () => reload() }))),
    h("section.panel",
      h("h3.panel-title", "Add your shop"),
      h("p.panel-hint",
        "Shopify, WooCommerce or Bol.com — anything that publishes product "
        + "pages. We only crawl public pages, we respect robots.txt, and we "
        + "identify ourselves honestly."),
      shopForm),
    h("section.panel",
      h("h3.panel-title", "Add a competitor"),
      h("p.panel-hint",
        "Paste a competitor's shop URL. We suggest some once your shop is "
        + "added, but we never crawl anyone you have not confirmed."),
      competitorForm),
    listHost);

  /* --------------------------------------------------------------- render */
  // One sentence per tier, on hover. The label itself is short enough to read
  // at a glance; this is for the customer who wants to know what the words mean.
  const SOURCE_EXPLAIN = {
    feed: "Read from the shop's own product feed: the exact price they charge, no interpretation.",
    jsonld: "Read from schema.org structured data on the product page: exact, machine-readable.",
    html: "Read from the rendered page: a best effort, and occasionally wrong.",
  };

  function competitorRow(competitor) {
    return h("li.competitor",
      h("div.competitor-main",
        h("p.competitor-name", competitor.name || competitor.domain),
        h("p.competitor-meta",
          h("a", {
            href: competitor.url, target: "_blank", rel: "noopener noreferrer",
          }, competitor.domain),
          " · ", platformLabel(competitor.platform),
          competitor.last_crawled_at
            ? h("span", " · crawled ", ago(competitor.last_crawled_at))
            : h("span", " · not crawled yet"))),
      h("div.competitor-side",
        competitor.last_status
          ? badge(competitor.last_status, crawlTone(competitor.last_status))
          : badge("pending", "neutral"),
        h("span.competitor-count", `${competitor.product_count} products`),
        // How the prices were read. "Exact, from their feed" is a materially
        // stronger claim than "we read the page", and the customer is entitled
        // to know which one they are looking at before they act on a number.
        competitor.last_crawled_at && competitor.source_label
          ? h("span.competitor-source", {
              class: `source-${competitor.data_source || "html"}`,
              title: SOURCE_EXPLAIN[competitor.data_source] || "",
            }, competitor.source_label)
          : null,
        h("div.row-actions",
          button("Crawl now", {
            size: "sm",
            onClick: (e) => withBusy(e.currentTarget,
              () => api.crawlCompetitor(competitor.id), "Crawl queued."),
          }),
          button("Remove", { size: "sm", onClick: () => removeCompetitor(competitor) }))));
  }

  function shopCard(shop) {
    const competitors = shop.competitors || [];
    return h("section.shop-card",
      h("header.shop-head",
        h("div",
          h("h3.shop-name", shop.name),
          h("p.shop-meta",
            h("a", { href: shop.url, target: "_blank", rel: "noopener noreferrer" },
              shop.domain),
            " · ", platformLabel(shop.platform),
            shop.last_crawled_at
              ? h("span", " · last crawl ", ago(shop.last_crawled_at))
              : h("span", " · never crawled"))),
        h("div.row-actions",
          button("Suggest", { size: "sm", onClick: (e) => discover(shop, e.currentTarget) }),
          button("Crawl now", {
            size: "sm",
            variant: "primary",
            onClick: (e) => withBusy(e.currentTarget,
              () => api.crawlShop(shop.id), "Crawl queued."),
          }),
          button("Write report", {
            size: "sm",
            onClick: (e) => withBusy(e.currentTarget,
              () => api.generateReport(shop.id, 7), "Report queued."),
          }),
          button("Delete", { size: "sm", onClick: () => removeShop(shop) }))),
      competitors.length
        ? h("ul.competitor-list", competitors.map(competitorRow))
        : h("p.shop-empty",
            "No competitors yet. Add one above, or let us suggest some — we "
            + "will not crawl anything until you confirm."));
  }

  /* ------------------------------------------------------------- actions */
  /** Disable a button, run work, then always restore it and refresh. */
  async function withBusy(button, work, successMessage) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Working…";
    try {
      await work();
      toast(successMessage, "success");
      reload();
    } catch (error) {
      toast(isPlanLimit(error) ? planLimitMessage(error) : error.message, "danger");
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function discover(shop, button) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Searching…";
    try {
      const result = await api.discoverCompetitors(shop.id);
      if (!result.suggestions.length) {
        toast("We could not find anything that looks like a competitor.", "info");
        return;
      }
      const ok = await confirmDialog({
        title: `Possible competitors for ${shop.name}`,
        message: `${result.suggestions.map((s) => s.domain).join("\n")}\n\n`
          + "Add all of these?",
        confirmLabel: "Add them",
      });
      if (ok) await addAll(shop, result.suggestions);
    } catch (error) {
      toast(error.message, "danger");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function addAll(shop, suggestions) {
    let added = 0;
    for (const suggestion of suggestions) {
      try {
        await api.createCompetitor({ url: suggestion.url, shop_id: shop.id });
        added += 1;
      } catch {
        // One failure (a duplicate, or a plan limit) must not abort the rest.
        // The customer can add the remainder by hand.
      }
    }
    toast(`Added ${added} competitor${added === 1 ? "" : "s"}`, "success");
    reload();
  }

  async function removeCompetitor(competitor) {
    const ok = await confirmDialog({
      title: "Stop tracking this competitor?",
      message: `We will stop crawling ${competitor.domain}. Its past alerts and `
        + "captures are deleted too. This cannot be undone.",
      confirmLabel: "Stop tracking",
    });
    if (!ok) return;
    try {
      await api.deleteCompetitor(competitor.id);
      toast("Competitor removed", "success");
      reload();
    } catch (error) {
      toast(error.message, "danger");
    }
  }

  async function removeShop(shop) {
    const ok = await confirmDialog({
      title: "Delete this shop?",
      message: `This deletes ${shop.name}, every competitor attached to it, all `
        + "captured products, alerts and reports. This cannot be undone.",
      confirmLabel: "Delete shop",
    });
    if (!ok) return;
    try {
      await api.deleteShop(shop.id);
      toast("Shop deleted", "success");
      reload();
    } catch (error) {
      toast(error.message, "danger");
    }
  }

  /* ----------------------------------------------------------------- load */
  async function render() {
    const { shops } = await api.shops();

    fill(shopPicker,
      h("option", { value: "" }, "Not attached to a specific shop"),
      shops.map((s) => h("option", { value: String(s.id) }, s.name)));

    if (!shops.length) {
      fill(listHost, empty({
        iconName: "shop",
        title: "No shops yet",
        message: "Add your shop above. Then add the competitors you compete "
          + "with, and we will start watching them.",
      }));
      return;
    }

    // The list endpoint returns counts only; the detail endpoint has the
    // competitor rows. One failure must not blank the whole screen.
    const details = await Promise.all(
      shops.map((s) => api.shop(s.id).catch(() => ({ ...s, competitors: [] }))),
    );
    fill(listHost, details.map(shopCard));
  }

  reload = asyncPanel(listHost, render, render);
  return reload;
}
