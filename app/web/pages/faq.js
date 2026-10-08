// Moved out of faq.html so the Content-Security-Policy can forbid inline scripts.
import { t, translateDom, langSwitch } from "/app/i18n.js";
translateDom();
document.title = t("fq.title");
document.getElementById("lp-tools")?.prepend(langSwitch());
