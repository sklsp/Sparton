// Public text pages (legal drafts, FAQ, pricing, errors): translate the chrome and add the switch.
// Long texts carry both languages in the HTML (`data-lang`); i18n.js sets <html lang>, CSS shows one.
import { translateDom, langSwitch } from "/app/i18n.js";

translateDom();
document.getElementById("lp-tools")?.prepend(langSwitch());
