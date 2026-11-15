# Sparton run quality-11

- [x] E1. server: public pages get the site address filled in (995 passed, 16 skipped, 1 warning in 69.65s (0:01:09))
- [x] E2. pages: canonical and og:url on every public page (997 passed, 16 skipped, 1 warning in 69.04s (0:01:09))
- [x] E3. pages: social cards load from the full address (1000 passed, 16 skipped, 1 warning in 69.48s (0:01:09))
- [x] E4. pages: one h1, a title and a description everywhere (1001 passed, 16 skipped, 1 warning in 69.98s (0:01:09))
- [x] E5. docs: APP_URL must be the public address (1001 passed, 16 skipped, 1 warning in 69.51s (0:01:09))

## Run summary

All five items done; final gate: 1001 passed, 16 skipped.

- E1: the server fills APP_URL into the landing pages and og-card.html (canonical, og:url, og:image), so no %APP_URL% placeholder reaches a browser.
- E2: every sitemap page carries its own canonical link and og:url; error pages stay noindex without one.
- E3: social cards reference the image by full address and it is served from /app/og-image.png.
- E4: privacy/terms had two h1s (one per language article); my initial fix demoted the Dutch title to h2, but a follow-up commit (e1e2f30) restored both because CSS hides one language at a time — each language keeps its own h1 and the structure test counts exactly one per language article on bilingual pages; dpa.html got a meta description; new html.parser test checks all ten public pages for title, description, lang and alt on every img.
- E5: DEPLOYMENT.md says APP_URL must be the exact public address (https, no trailing slash) because canonical links and social cards use it.
