"""Dutch and English are both first-class: every key exists in both, and every key the code
asks for exists at all. A missing key shows its raw name to a customer, so this is a test,
not a lint."""

from __future__ import annotations

import json
import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "app" / "web"


def _load(lang: str) -> dict[str, str]:
    return json.loads((WEB / "i18n" / f"{lang}.json").read_text(encoding="utf-8"))


def test_every_key_exists_in_both_languages():
    en, nl = _load("en"), _load("nl")
    assert sorted(set(en) - set(nl)) == [], "keys missing in nl.json"
    assert sorted(set(nl) - set(en)) == [], "keys missing in en.json"


def test_no_translation_is_empty():
    for lang in ("en", "nl"):
        empty = [k for k, v in _load(lang).items() if not str(v).strip()]
        assert not empty, f"empty strings in {lang}.json: {empty}"


def test_placeholders_match_between_languages():
    en, nl = _load("en"), _load("nl")
    for key in en:
        assert set(re.findall(r"\{(\w+)\}", en[key])) == set(re.findall(r"\{(\w+)\}", nl[key])), key


def test_every_key_the_code_uses_exists():
    """Static `t("…")` calls and `data-i18n="…"` attributes. Keys built at runtime
    (`nav.${id}`) are checked through their known prefixes below."""
    en = _load("en")
    used: set[str] = set()
    for path in [*WEB.glob("*.js"), *WEB.glob("views/*.js"), *WEB.glob("*.html")]:
        text = path.read_text(encoding="utf-8")
        used |= set(re.findall(r"""\bt\(\s*["']([\w.]+)["']""", text))
        used |= set(re.findall(r'data-i18n(?:-html)?="([\w.]+)"', text))
        used |= {k for pair in re.findall(r'data-i18n-attr="([^"]+)"', text) for k in re.findall(r":([\w.]+)", pair)}
    missing = sorted(k for k in used if k not in en)
    assert not missing, f"keys used in code but missing from the dictionaries: {missing}"


def test_runtime_keys_exist():
    en = _load("en")
    routes = re.findall(r'id: "(\w+)"', (WEB / "routes.js").read_text(encoding="utf-8"))
    for route in routes:
        assert f"nav.{route}" in en, f"nav.{route}"
    for scene in ("shop", "discover", "match", "moves", "certainty", "report"):
        assert f"lp.rail.{scene}" in en
    for code in ("nl", "en"):
        assert f"lang.{code}" in en
