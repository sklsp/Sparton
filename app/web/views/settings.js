// Settings — the account screen.
//
// Three things live here and nothing else: who you are, your password, and the
// raw facts about what we are doing to your data. Adding a "preferences" tab
// full of toggles that change nothing is how a settings page starts lying.

import { api } from "../api.js";
import { h, fill, toast, badge, button, confirmDialog } from "../ui.js";

export default function settingsView(host, { state, onLogout }) {
  const user = state?.user || {};

  /* ------------------------------------------------------------- password */
  function passwordForm() {
    const current = h("input.input", {
      type: "password", name: "current", required: true,
      autocomplete: "current-password",
    });
    const next = h("input.input", {
      type: "password", name: "next", required: true, minlength: "8",
      autocomplete: "new-password",
    });
    const confirm = h("input.input", {
      type: "password", name: "confirm", required: true, minlength: "8",
      autocomplete: "new-password",
    });
    const message = h("p.form-message", { role: "status" });
    message.hidden = true;
    const submit = button("Change password", { type: "submit", variant: "primary" });

    const form = h("form.form-grid", {
      onsubmit: async (event) => {
        event.preventDefault();
        message.hidden = true;

        // Checked here purely to save a round trip; the server checks it too.
        if (next.value !== confirm.value) {
          message.dataset.tone = "danger";
          message.textContent = "The two new passwords do not match.";
          message.hidden = false;
          return;
        }
        if (next.value.length < 8) {
          message.dataset.tone = "danger";
          message.textContent = "Use at least 8 characters.";
          message.hidden = false;
          return;
        }

        submit.disabled = true;
        submit.textContent = "Changing…";
        try {
          await api.changePassword(current.value, next.value);
          form.reset();
          toast("Password changed", "success");
        } catch (error) {
          message.dataset.tone = "danger";
          message.textContent = error.message;
          message.hidden = false;
        } finally {
          submit.disabled = false;
          submit.textContent = "Change password";
        }
      },
    },
      h("label.field", h("span", "Current password"), current),
      h("label.field", h("span", "New password"), next),
      h("label.field", h("span", "Confirm new password"), confirm),
      h("div.form-actions", submit));

    return h("div.stack", form, message);
  }

  /* --------------------------------------------------------------- account */
  function accountCard() {
    return h("div.account-card",
      h("dl.account-facts",
        h("div", h("dt", "Email"), h("dd", user.email || "—")),
        h("div", h("dt", "Role"), h("dd", user.role || "member")),
        h("div", h("dt", "Organisation"),
          h("dd", user.organization_name || (user.organization_id
            ? `#${user.organization_id}`
            : "—"))),
        h("div", h("dt", "Email verified"),
          h("dd", user.email_verified
            ? badge("Verified", "success")
            : badge("Not verified", "warning")))));
  }

  /* ------------------------------------------------------------------ data */
  const facts = [
    ["What we crawl", "Only public product pages of shops and competitors you "
      + "have added yourself."],
    ["robots.txt", "Respected for every request. A page that forbids us is "
      + "skipped, not worked around."],
    ["How we identify", "Requests carry an honest user agent and a contact URL, "
      + "so a site owner can reach us or block us."],
    ["What we store", "Product name, price, availability and stock, per crawl, "
      + "as a time series. We do not store page HTML."],
    ["Alerts", "Computed by comparing two captures. Numbers are arithmetic, not "
      + "model output, so they are reproducible."],
    ["Reports", "Every figure comes from that same capture history."],
  ];

  function dataCard() {
    return h("div.data-card",
      h("dl.data-facts", facts.map(([label, text]) =>
        h("div", h("dt", label), h("dd", text)))));
  }

  host.append(
    h("div.toolbar", h("h2.view-title", "Settings")),
    h("section.panel",
      h("h3.panel-title", "Your account"),
      accountCard()),
    h("section.panel",
      h("h3.panel-title", "Password"),
      h("p.panel-hint",
        "You will stay signed in on this device after changing it. Other "
        + "devices keep working until their sessions expire."),
      passwordForm()),
    h("section.panel",
      h("h3.panel-title", "What SPARTON does with your data"),
      dataCard()),
    h("section.panel",
      h("h3.panel-title", "Session"),
      h("p.panel-hint", "Sign out of this device."),
      h("div.form-actions",
        button("Sign out", { onClick: signOut }))));

  /** Revoke the session server-side, then drop the local token. */
  async function signOut() {
    const ok = await confirmDialog({
      title: "Sign out?",
      message: "You will need to sign in again on this device.",
      confirmLabel: "Sign out",
    });
    if (!ok) return;
    await api.logout().catch(() => {});
    api.token.clear();
    onLogout?.();
  }
}
