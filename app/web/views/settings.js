// Account settings: who you are, your password, your language, signing out.

import { api, token } from "../api.js";
import { h, fill, button, toast } from "../ui.js";
import { t, langSwitch } from "../i18n.js";

export default function settingsView(host, { state }) {
  const user = state.user || {};
  fill(host,
    h("header.view-head", h("h1.view-title", t("nav.settings"))),
    h("section.plate.settings-block", { "aria-labelledby": "st-account" },
      h("h2#st-account", t("st.account")),
      h("dl.kv-list",
        h("div", h("dt", t("auth.email")), h("dd", user.email || "—")),
        h("div", h("dt", t("st.verified")), h("dd", t(user.email_verified ? "st.yes" : "st.no"))))),
    passwordBlock(),
    h("section.plate.settings-block", { "aria-labelledby": "st-lang" },
      h("h2#st-lang", t("lang.label")),
      h("p", t("st.langBody")),
      h("div.st-lang", langSwitch())),
    h("section.plate.settings-block", { "aria-labelledby": "st-out" },
      h("h2#st-out", t("nav.signout")),
      h("p", t("st.signoutBody")),
      button(t("nav.signout"), { variant: "danger", onClick: async () => {
        try { await api.logout(); } catch { /* the token may already be dead */ }
        token.clear();
        location.hash = "#/login";
        location.reload();
      } })));
}

function passwordBlock() {
  const field = (id, label, auto) => {
    const el = h("input.input", { id, type: "password", required: true, minlength: auto === "new-password" ? 8 : 1, autocomplete: auto });
    return [h("div.field", h("label", { for: id }, label), el), el];
  };
  const [curWrap, cur] = field("st-cur", t("st.currentPassword"), "current-password");
  const [nextWrap, next] = field("st-new", t("auth.newPassword"), "new-password");
  const msg = h("p.form-error", { role: "alert", hidden: true });
  const submit = button(t("auth.savePassword"), { type: "submit", variant: "primary" });
  return h("section.plate.settings-block", { "aria-labelledby": "st-pw" },
    h("h2#st-pw", t("st.password")),
    h("form.st-form", {
      onsubmit: async (e) => {
        e.preventDefault();
        msg.hidden = true;
        submit.dataset.loading = "true";
        try {
          await api.changePassword(cur.value, next.value);
          e.target.reset();
          toast(t("st.passwordSaved"), "success");
        } catch (err) {
          msg.textContent = err.status === 400 || err.status === 401 ? t("st.wrongPassword") : err.message;
          msg.hidden = false;
        } finally { delete submit.dataset.loading; }
      },
    }, curWrap, nextWrap, h("p.field-hint", t("auth.passwordHint")), msg, submit));
}
