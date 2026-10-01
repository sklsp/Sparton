"""Screenshot round for the frontend build (not a test; pytest ignores this file).

    python tests/qa_shots.py http://127.0.0.1:8002 / /app/#signup /app/#overview

Captures every path at 1440x900 and 375x812, in NL and EN, into qa_shots/. Paths under /app/
other than the auth hashes get a fresh signed-in account. Prints console errors and any
horizontal overflow at 375 px, the two checks a screenshot alone does not show.
"""

from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "qa_shots"
VIEWPORTS = {"desktop": (1440, 900), "mobile": (375, 812)}
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def slug(path: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", path.lower()).strip("-") or "home"


def main(base: str, paths: list[str], full: bool = True) -> int:
    OUT.mkdir(exist_ok=True)
    problems = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=CHROME if Path(CHROME).exists() else None)
        session_token = None
        for lang in ("nl", "en"):
            for name, (w, h) in VIEWPORTS.items():
                ctx = browser.new_context(viewport={"width": w, "height": h}, locale="en-US",
                                          reduced_motion="reduce")
                page = ctx.new_page()
                errors: list[str] = []
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(f"{base}/", wait_until="domcontentloaded")
                page.evaluate("l => localStorage.setItem('sparton.lang', l)", lang)
                for path in paths:
                    needs_session = path.startswith("/app/") and not re.search(r"#/?(signup|login|reset|forgot)", path)
                    if needs_session:
                        if session_token is None:
                            res = page.request.post(f"{base}/auth/register", data={
                                "email": f"qa-{uuid.uuid4().hex[:8]}@example.com",
                                "password": "correct-horse-battery", "organization_name": "QA demo shop"})
                            session_token = res.json()["token"]
                        page.evaluate("t => localStorage.setItem('sparton.token', t)", session_token)
                    else:
                        page.evaluate("() => localStorage.removeItem('sparton.token')")
                    page.goto("about:blank")  # a hash-only change would not reboot the SPA
                    errors.clear()
                    page.goto(f"{base}{path}", wait_until="networkidle")
                    page.wait_for_timeout(700)
                    file = OUT / f"{slug(path)}-{name}-{lang}.png"
                    page.screenshot(path=str(file), full_page=full)
                    overflow = page.evaluate("() => document.documentElement.scrollWidth - innerWidth")
                    note = []
                    if errors:
                        note.append(f"console errors: {errors}")
                    if overflow > 0:
                        note.append(f"horizontal overflow {overflow}px")
                    if note:
                        problems += 1
                    print(f"{file.name}: {'; '.join(note) or 'ok'}")
                ctx.close()
        browser.close()
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2:] or ["/"]))
