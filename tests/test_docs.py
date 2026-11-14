"""The docs must not drift from the code.

Items 1-4 of this run verified every concrete claim by hand; that verification
rots the moment someone renames a module or adds an endpoint. These checks
re-run it on every test run, for the documents that describe the system:

* every repository path written in backticks in README.md, docs/ARCHITECTURE.md
  and docs/DEPLOYMENT.md exists in the repo;
* every environment variable named in DEPLOYMENT.md's settings list is a real
  Settings field (or a compose-level variable);
* every D-NNN decision cited from code is defined in docs/DECISIONS.md.

The extraction rules below are deliberately conservative: when in doubt, the
span is checked rather than skipped, and every skip carries a comment saying
why it cannot be a repository path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.core.config import Settings

ROOT = Path(__file__).resolve().parent.parent

DOCS_WITH_PATHS = ("README.md", "docs/ARCHITECTURE.md", "docs/DEPLOYMENT.md")

# Backticked tokens that look like paths but are not files of this repository.
NON_REPO_PATHS = {
    # robots.txt is a web-standard filename referenced generically (competitor
    # sites, crawler behaviour); Sparton does not ship one.
    "robots.txt",
}

_SPAN_RE = re.compile(r"`([^`\n]+)`")
_GLOB_CHARS = set("*?[]<>${}")


def _is_path_candidate(span: str) -> bool:
    """A backticked span is a repository-path claim if it looks like one."""
    if any(ch.isspace() for ch in span):
        return False  # commands and prose, e.g. `docker compose logs api`
    if "://" in span or span.startswith(("http://", "https://")):
        return False  # URLs
    if _GLOB_CHARS.intersection(span):
        return False  # globs and placeholders such as <id>.json
    if span.startswith("/"):
        return False  # endpoint and container paths: /live, /app/data/outbox
    if re.fullmatch(r"\.[A-Za-z0-9]+", span):
        return False  # a bare extension such as .txt, not a filename
    looks_like_path = "/" in span or re.search(r"\.(py|md|yml|toml|ini|txt|js|html|css)$", span)
    if not looks_like_path:
        return False
    # A multi-segment token without an extension whose first segment is not a
    # top-level entry of this repo names something else entirely (a model id
    # such as sentence-transformers/all-MiniLM-L6-v2, an owner/repo such as
    # sklsp/shopfeed). Bare filenames are still checked: they must live at the
    # repo root.
    if "/" in span and not re.search(r"\.[A-Za-z0-9]+$", span):
        first = span.split("/", 1)[0]
        if first not in os.listdir(ROOT):
            return False
    return True


def _exists_in_repo(span: str) -> bool:
    """True if the path exists, or names a module (extensionless `app/llm`)."""
    target = ROOT / span
    if target.exists():
        return True
    if "." not in os.path.basename(span):
        return (ROOT / (span + ".py")).exists()
    return False


def _spans_outside_foreign_tables(text: str) -> list[str]:
    """Backticked path candidates, ignoring column 0 of the mapping tables.

    docs/ARCHITECTURE.md sections 9-10 map paths from the read-only Apollo and
    Ares reference copies onto Sparton modules. Their first column lives in
    other codebases and can never exist here; the SPARTON columns are checked.
    """
    spans: list[str] = []
    header_first_cell = ""
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            header_first_cell = ""
            spans.extend(_SPAN_RE.findall(line))
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header_first_cell == "":  # first row of a table: the header
            header_first_cell = cells[0]
            continue
        if header_first_cell in ("Apollo", "Ares"):
            spans.extend(_SPAN_RE.findall(" ".join(cells[1:])))
        else:
            spans.extend(_SPAN_RE.findall(line))
    return [s for s in spans if _is_path_candidate(s)]


def test_backticked_repo_paths_exist() -> None:
    """Every backticked repository path in the three docs exists on disk."""
    missing = []
    for name in DOCS_WITH_PATHS:
        text = (ROOT / name).read_text(encoding="utf-8")
        for span in _spans_outside_foreign_tables(text):
            if span in NON_REPO_PATHS:
                continue
            if not _exists_in_repo(span):
                missing.append(f"{name}: `{span}`")
    assert not missing, "Docs reference paths that do not exist in the repo:\n  " + "\n  ".join(missing)


def test_deployment_env_vars_are_settings_fields() -> None:
    """Every variable in DEPLOYMENT.md's settings list is a real setting.

    The list is the "settings that matter" subsections, each headed by an
    ALL_CAPS variable name (optionally with its example value). Most are read
    by app.core.config.Settings; POSTGRES_PASSWORD is consumed by
    docker-compose.yml instead, so compose-referenced variables also pass.
    """
    text = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    named = re.findall(r"^###\s+`([A-Z][A-Z0-9_]*)(?:=[^`]*)?`\s*$", text, flags=re.M)
    assert named, "no settings list found in docs/DEPLOYMENT.md - did the heading format change?"
    fields = {name.lower() for name in Settings.model_fields}
    compose_text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    compose_vars = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_text))
    unknown = [v for v in named if v.lower() not in fields and v not in compose_vars]
    assert not unknown, (
        f"docs/DEPLOYMENT.md names variables that are neither Settings fields nor compose vars: {unknown}"
    )


def test_decision_references_exist_in_decisions_doc() -> None:
    """Every D-NNN cited from code is defined as a heading in docs/DECISIONS.md."""
    decisions = (ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    defined = set(re.findall(r"^##\s+D-(\d{3})\b", decisions, flags=re.M))
    cited: dict[str, list[str]] = {}
    for top in ("app", "workers", "tests"):
        for path in sorted((ROOT / top).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for num in re.findall(r"\bD-(\d{3})\b", path.read_text(encoding="utf-8")):
                cited.setdefault(num, []).append(str(path.relative_to(ROOT)))
    missing = {num: files for num, files in sorted(cited.items()) if num not in defined}
    detail = "\n  ".join(f"D-{num} cited from {', '.join(files)}" for num, files in missing.items())
    assert not missing, f"Code cites decisions that docs/DECISIONS.md does not define:\n  {detail}"

