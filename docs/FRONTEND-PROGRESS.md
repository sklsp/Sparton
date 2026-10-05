# Sparton v1.0 frontend: progress

Resume point for the scheduled frontend sessions. One line per milestone.

✅ M1 Direction (Price Board), tokens, NL/EN i18n, dashboard shell | fac114d | landing page body still old (M2); views still old components (M5/M6)
✅ M2 Landing: live demo board, scroll story, plans from API, FAQ, CTA | e389e6d | none
✅ M3 Auth: sign in, start free, forgot/reset, email confirm (+ /verify-email, /reset-password routes in c44651e) | 7553393 | server error texts English-only, mapped for known cases
✅ M4 Onboarding: shop URL → competitors → first check with live per-competitor progress | 948ab34 | discovery uses live web search
✅ M5 This week board, competitors, product price history (+ GET /changes/{id}/history) | 6560356 | own price not tracked by backend (D-031)
✅ M6 Alerts, reports, billing, settings with empty/loading/error states | 20cb4a4 | report prose English-only; email digest not built (D-032)
✅ M7 Sparton vs Prisync (NL+EN, sourced) | c44651e | single URL with language switch
✅ M8 NAMING.md (top 3: Flapbord, Peilbord, Klapbord) | 1a6ca78 | trademarks [unverified]
✅ M9 Finish review (2 rounds, fixes applied), DESIGN.md, Lighthouse 100/100/100 landing+signup, full suite 530 passed | 5ed7e75 | reviewer's last 3 micro items applied unscored; report prose English-only
✅ M10 Report email (NL+EN, 600px, images off, dark mode) + in-app report view from M6 | 182df9a | not wired to sending (D-032)
✅ M11 /pricing, /faq, 404/500, draft privacy+terms NL+EN (DPA restyled, EN only) | b5a76ee | legal placeholders for controller, processors, law
✅ M12 Three NL search pages + sitemap.xml + robots.txt | 050817a | sitemap uses APP_URL; 'exact' claims depend on shopfeed being in the image (release blocker)
