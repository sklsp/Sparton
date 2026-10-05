"""The product must not depend on the experimental domains'' libraries.

The image was 9.8 GB because `requirements.txt` carried
sentence-transformers, faiss-cpu and the PyTorch stack for the feature-flagged
documents/RAG, generation, datasets and training domains -- none of which the
shipped product imports. Worse, `app/main.py` and `app/api/__init__.py` imported
those routers unconditionally, so `import app.main` reached torch even in a
container that would never serve a single document route.

These tests are the guard. They do not check that a list looks tidy: they check
the import graph of the product really is free of the heavy libraries, by
running the import in a subprocess where those libraries are *unimportable* --
simulating a machine that does not have them, rather than trusting that this one
happens not to use them.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: Modules that mean "this import needs a GPU runtime to succeed".
HEAVY_MODULES = {"faiss", "torch", "sentence_transformers", "transformers"}

#: The libraries the experimental domains need, in full.
EXPERIMENTAL_IMPORTS = HEAVY_MODULES | {"PIL", "pypdf", "docx"}

#: Packages behind flags that are off by default.
EXPERIMENTAL_PACKAGES = {"documents", "datasets", "generation", "training"}

#: The product's own packages. A file under one may not reach the heavy libs.
PRODUCT_PACKAGES = {"agent", "api", "billing", "core", "ecommerce", "research", "web"}

#: The experimental routers live in app/api alongside the shared ones.
EXPERIMENTAL_FILES = {"create.py", "knowledge.py"}

#: A meta-path finder that refuses the heavy libraries. `find_spec` is the
#: current protocol; the legacy find_module/load_module pair was removed in
#: Python 3.12, and a blocker written against it silently does nothing -- which
#: would make every subprocess test below vacuous.
BLOCKER = """
from importlib.abc import MetaPathFinder

BLOCKED = {blocked!r}


class Block(MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("No module named " + repr(name))
        return None


import sys

sys.meta_path.insert(0, Block())
"""


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
    )


def _module_level_imports(path: Path) -> set[str]:
    """Top-level modules imported at *module scope*.

    Parsed rather than imported, so this still works on a machine that does not
    have the library. Module scope only: a lazy import inside a function body
    costs nothing until that function is called, and the codebase already does
    this correctly in app/agent/tools.py.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in tree.body:  # not ast.walk, which would descend into functions
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _product_files() -> list[Path]:
    return [
        p
        for p in (ROOT / "app").rglob("*.py")
        if p.parent.name in PRODUCT_PACKAGES and p.name not in EXPERIMENTAL_FILES
    ]


def _requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TestTheBlockerItselfWorks:
    """A test that cannot fail is worse than no test, so the guard is checked
    before anything relies on it."""

    def test_the_meta_path_blocker_actually_blocks(self):
        result = _run(BLOCKER.format(blocked=["json"]) + "import json\n")
        assert result.returncode != 0, "the blocker let a blocked import through"
        assert "No module named" in result.stderr, result.stderr

    def test_the_blocker_finds_spec_not_the_removed_legacy_protocol(self):
        assert "find_spec" in BLOCKER
        assert "find_module" not in BLOCKER, (
            "the legacy meta-path protocol was removed in Python 3.12; a blocker "
            "using it silently allows everything"
        )


class TestTheProductImportsNothingHeavy:
    def test_no_product_module_imports_a_heavy_library(self):
        offenders = [
            f"{p.relative_to(ROOT)} imports {sorted(_module_level_imports(p) & HEAVY_MODULES)}"
            for p in _product_files()
            if _module_level_imports(p) & HEAVY_MODULES
        ]
        assert not offenders, offenders

    def test_no_product_module_imports_an_experimental_package(self):
        offenders = [
            f"{p.relative_to(ROOT)} imports {package}"
            for p in _product_files()
            for package in _module_level_imports(p) & EXPERIMENTAL_PACKAGES
        ]
        assert not offenders, offenders

    def test_importing_the_app_with_no_extras_still_serves_the_product(self):
        """The end-to-end proof, and the one that actually caught it."""
        script = (
            BLOCKER.format(blocked=sorted(EXPERIMENTAL_IMPORTS))
            + "import sys\n"
            "import app.main\n"
            "app = app.main.create_app()\n"
            "paths = set(app.openapi()['paths'])\n"
            "assert '/shops' in paths, 'the product lost its own routes'\n"
            "assert 'faiss' not in sys.modules, 'faiss was imported'\n"
            "assert 'sentence_transformers' not in sys.modules\n"
            "print('OK', len(paths))\n"
        )
        result = _run(script)
        assert result.returncode == 0, (
            "importing the app needs the experimental libraries:\n"
            + result.stderr[-2500:]
        )
        assert "OK" in result.stdout, result.stdout

    def test_lazy_imports_inside_functions_are_allowed(self):
        """app/agent/tools.py imports the RAG service inside the tool that uses
        it. That is free at import time, and the guard must not regress on it."""
        source = (ROOT / "app" / "agent" / "tools.py").read_text(encoding="utf-8")
        assert re.search(r"^\s+from app\.documents\.", source, re.M), (
            "the agent's document tool no longer imports lazily, so the product "
            "would load faiss at start again"
        )


class TestDisabledDomainsAreNotEvenImported:
    def test_main_does_not_import_the_experimental_routers(self):
        import app.main

        source = Path(app.main.__file__).read_text(encoding="utf-8")
        top_level = source.split("def create_app", 1)[0]
        for router in ("knowledge", "create"):
            assert not re.search(rf"^\s*from app\.api import [^\n]*\b{router}\b", top_level, re.M), (
                f"{router} is imported at module level in app/main.py"
            )

    def test_the_api_package_does_not_reexport_them(self):
        """`app/api/__init__.py` re-exporting them silently undid the lazy
        import: `from app.api import health` executes the package __init__
        first, and the product was back to loading faiss."""
        source = (ROOT / "app" / "api" / "__init__.py").read_text(encoding="utf-8")
        body = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        for router in ("knowledge", "create"):
            assert not re.search(rf"^\s*from app\.api import [^\n]*\b{router}\b", body, re.M), (
                f"app/api/__init__.py re-exports {router}, so every `from app.api "
                "import x` pulls in the experimental dependencies"
            )

    def test_a_default_app_has_no_experimental_routes(self):
        from app.core.config import settings
        from app.main import create_app

        assert "documents" not in settings.enabled_domains.split(","), (
            "the default configuration must not enable an experimental domain"
        )
        paths = create_app().openapi()["paths"]
        assert not any(p.startswith(("/documents", "/rag", "/datasets")) for p in paths)


class TestExperimentalDomainsFailLoudlyWithoutTheirDeps:
    def test_enabling_documents_without_the_extras_refuses_to_start(self):
        """Better a deploy that stops with a traceback than an app whose RAG
        routes 500 on the first request. The operator reads the missing package
        name in the deploy log instead of hearing about it from a customer."""
        script = (
            "import os, sys\n"
            "os.environ['ENABLED_DOMAINS'] = "
            "'intelligence,commerce,agent,documents'\n"
            + BLOCKER.format(blocked=sorted(EXPERIMENTAL_IMPORTS))
            # The import sits inside the try on purpose: with the routers
            # imported lazily the failure lands on `import app.main` rather
            # than inside create_app(), which is better -- no app exists at all.
            + "try:\n"
            "    from app.main import create_app\n"
            "    create_app()\n"
            "except ImportError as exc:\n"
            "    print('RAISED', exc)\n"
            "else:\n"
            "    print('STARTED')\n"
        )
        result = _run(script)
        assert "RAISED" in result.stdout, (
            "enabling `documents` without the extras started the app anyway\n"
            + result.stdout[-1200:] + result.stderr[-1200:]
        )
        assert any(n in result.stdout.lower() for n in ("faiss", "sentence")), (
            f"the error does not name the missing package: {result.stdout}"
        )


class TestTheRequirementsAreSplit:
    @pytest.fixture(scope="class")
    def product(self) -> str:
        return " ".join(_requirement_lines(ROOT / "requirements.txt")).lower()

    @pytest.fixture(scope="class")
    def experimental(self) -> str:
        return " ".join(_requirement_lines(ROOT / "requirements-experimental.txt")).lower()

    def test_the_heavy_libraries_are_not_in_the_product_set(self, product):
        for library in ("sentence-transformers", "faiss", "torch", "pillow", "numpy"):
            assert library not in product, (
                f"{library} is in requirements.txt: it is an experimental extra "
                "and the image is built from that file"
            )

    def test_the_extras_file_carries_them(self, experimental):
        for library in ("sentence-transformers", "faiss-cpu", "numpy", "pillow"):
            assert library in experimental, f"{library} is missing from the extras file"

    def test_the_extras_file_builds_on_the_product(self):
        assert "-r requirements.txt" in _requirement_lines(
            ROOT / "requirements-experimental.txt"
        ), "the extras file must extend the product set, not duplicate it"

    def test_the_product_still_declares_what_it_imports(self, product):
        for library in (
            "fastapi", "uvicorn", "sqlalchemy", "alembic", "psycopg", "redis",
            "httpx", "requests", "opentelemetry", "python-multipart",
            "python-dotenv", "pytest", "pyyaml",
        ):
            assert library in product, f"{library} left the product requirements"

    def test_nothing_is_declared_twice_in_the_product_set(self):
        names = [
            line.split(">=")[0].split("==")[0].strip().lower()
            for line in _requirement_lines(ROOT / "requirements.txt")
        ]
        assert len(names) == len(set(names)), "a duplicate requirement"


class TestTheImageInstallsOnlyTheProductSet:
    @pytest.fixture(scope="class")
    def dockerfile(self) -> str:
        return (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_the_extras_are_opt_in(self, dockerfile):
        assert re.search(r"ARG\s+INSTALL_EXPERIMENTAL=0", dockerfile), (
            "the experimental extras must be off by default"
        )

    def test_the_extras_file_is_copied_for_the_conditional_install(self, dockerfile):
        assert re.search(
            r"COPY\s+requirements\.txt\s+requirements-experimental\.txt", dockerfile
        ), "the Dockerfile references a file it never copies"

    def test_the_product_install_is_not_gated(self, dockerfile):
        """An accident that put the product install behind the flag would ship an
        image that starts and then fails on the first request."""
        body = dockerfile.split("ARG INSTALL_EXPERIMENTAL", 1)[-1]
        assert re.search(r"pip install [^\n]*-r requirements\.txt", body)
