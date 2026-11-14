# Sparton run quality6

- [x] D1. docs: README claims match the code (983 passed, 16 skipped, 2 warnings in 66.99s (0:01:06))
- [x] D2. docs: ARCHITECTURE claims match the code (983 passed, 16 skipped, 2 warnings in 68.04s (0:01:08))
- [x] D3. docs: DEPLOYMENT claims match the code (983 passed, 16 skipped, 2 warnings in 66.89s (0:01:06))
- [ ] D4. docs: DECISIONS are referenced correctly
- [ ] D5. docs: label the historical documents
- [ ] D6. tests: doc paths and settings cannot drift

## Run 2026-11-14 (items 1-6 per 00-start.md)

Verified against code + a full local suite run (.venv: no numpy/playwright/PIL, shopfeed installed):

- Item 1 (README CI counts): STALE. README said "549 tests pass in CI" (line 18) and
  "In CI that is 516 tests" (line 208). Reality: collect-only = 996 tests, deterministic
  (ran twice); full run = 983 passed + 16 skipped in ~68 s. The 16 skips include the three
  module-level importorskip failures (report_email/playwright, browser_smoke/playwright,
  experimental_domains/numpy) that pytest counts as one skip each at runtime (+3 vs collected).
  CI test job installs only requirements.txt + pytest-cov -> same 996; browser job runs the
  33 smoke tests separately (verified: 30 functions, parametrize expands to exactly 33).
  FIXED: line 18 now "CI runs 996 tests, plus 33 more that drive a real browser...";
  line 208 now "In CI that is 996 tests".
- Item 2 (ARCHITECTURE.md app/web/js/ + app/web/static/): NOT PRESENT in the current file.
  ARCHITECTURE.md has no frontend section at all (only two "web search" mentions). Nothing to fix.
- Item 3 (README app/web/static/): NOT PRESENT. README only says "static ES-module SPA" /
  "static SPA" (meaning: no build step) - accurate. Nothing to fix.
- Item 4 (README JS modules under app/web/js/): NOT PRESENT. Verified reality: dashboard is
  top-level ES modules in app/web/ + views/ (+ pages/, vendor/); index.html loads ./app.js;
  zero references to ../js/ or js/dom across all 31 JS files; no js/ dir exists. Nothing to fix.
- Item 5 (PROGRESS.md V10 + V11-PROGRESS.md): TRUE as intended - PROGRESS.md ends at
  "Phase 10" and docs/V11-PROGRESS.md ("v1.1 progress", M1-M5) exists. Nothing to fix.
- Item 6: the only error found was the stale counts (item 1); fixed. Adjacent claims re-checked
  and accurate: requirements-dev.txt, .coveragerc fail_under=85, mypy.ini all exist; CI test job
  = requirements.txt + pytest-cov -> pytest --cov; browser job separate with playwright.

Suite evidence: `983 passed, 16 skipped in 67.95s` (local, .venv). Scratch outputs removed.

