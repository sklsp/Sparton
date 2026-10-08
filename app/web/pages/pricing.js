// Moved out of pricing.html so the Content-Security-Policy can forbid inline scripts.
import { t, translateDom, langSwitch } from "/app/i18n.js";
import { planBoard } from "/app/plans.js";
translateDom();
document.title = t("pp.title");
document.getElementById("lp-tools")?.prepend(langSwitch());
const host = document.getElementById("plans");
fetch("/billing/plans").then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); }).then(({ plans }) => {
  host.replaceChildren(...plans.map((p) => {
    const a = document.createElement("a");
    a.className = `btn plan-cta${p.id === "pro" ? " btn-primary" : ""}`;
    a.href = p.id === "free" ? "/app/#signup" : `/app/#signup?plan=${encodeURIComponent(p.id)}`;
    a.textContent = p.id === "free" ? t("lp.cta") : t("plan.choose", { name: p.name });
    return planBoard(p, { featured: p.id === "pro", action: a });
  }));
}).catch(() => { host.textContent = t("lp.plans.error"); });
