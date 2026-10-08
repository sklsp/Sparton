// Moved out of vs-prisync.html so the Content-Security-Policy can forbid inline scripts.
import { translateDom, langSwitch, fmtMoney, t } from "/app/i18n.js";
translateDom();
document.getElementById("lp-tools")?.prepend(langSwitch());
document.title = t("vs.metaTitle");
// Our own prices come from the API, never from this page.
fetch("/billing/plans").then((r) => r.json()).then(({ plans }) => {
  const free = plans.find((p) => p.price_cents === 0);
  if (free) document.getElementById("vs-r1").textContent = t("vs.r1.us", {
    price: fmtMoney(0, "EUR", 0), shops: t("plan.shops", { n: free.shops }), competitors: t("plan.competitors", { n: free.competitors }),
  });
}).catch(() => {});
