"""The deployment artifacts must match the application they deploy.

None of this is exercised by a request test, and all of it fails the same way:
the first person to find out is a customer, at deploy time, in production. A
Dockerfile that copies a directory that no longer exists, a compose file with a
typo in an env var, or a healthcheck pointed at a route that was renamed are
all silent until that moment.

These parse the files rather than building the image — no Docker daemon is
available here — and assert the things that actually break.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERIGNORE = ROOT / ".dockerignore"
ENV_EXAMPLE = ROOT / ".env.example"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


class TestDockerfile:
    def test_every_instruction_is_real(self, dockerfile):
        """A typo like `RUNN` fails at build time, on someone else's machine."""
        directives = (
            "FROM", "RUN", "ENV", "COPY", "WORKDIR", "USER", "EXPOSE",
            "HEALTHCHECK", "CMD", "ENTRYPOINT", "ARG", "LABEL", "ADD", "SHELL",
        )
        joined = dockerfile.replace("\\\n", " ")
        for number, line in enumerate(joined.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            assert stripped.startswith(directives), f"line {number}: {stripped[:70]}"

    def test_no_comment_sits_inside_an_env_block(self, dockerfile):
        """`ENV A=1 \\` followed by a `#` line is a syntax error, not a comment."""
        offenders = [
            line.strip() for line in dockerfile.splitlines()
            if line.strip().startswith("ENV") and "#" in line
        ]
        assert not offenders, offenders

    def test_every_copied_path_exists(self, dockerfile):
        """`COPY app/ ./app/` is silent at review time and fatal at build time."""
        for source in re.findall(r"^COPY\s+(?!--from)(\S+)", dockerfile, re.M):
            source = source.rstrip("/")
            if source in (".", "./"):
                continue
            assert (ROOT / source).exists(), f"COPY {source} but {source} does not exist"

    def test_the_image_does_not_run_as_root(self, dockerfile):
        assert re.search(r"^USER\s+(?!root)\S+", dockerfile, re.M), (
            "the image still runs as root"
        )

    def test_migrations_run_before_the_server(self, dockerfile):
        """Serving requests against a schema that lags the code is the most
        common way an app fails to boot. Migrations run in the same command."""
        # The CMD instruction only, so the explanatory comment above it — which
        # mentions `|| true` to say why it is absent — is not read as the thing.
        cmd = dockerfile.split("\nCMD", 1)[-1]
        assert "alembic upgrade head" in cmd
        assert cmd.index("alembic upgrade head") < cmd.index("uvicorn")
        # `|| true` would hide a failed migration behind a running process.
        assert "|| true" not in cmd

    def test_the_healthcheck_points_at_a_real_route(self, dockerfile):
        """The health router is mounted unprefixed, so the path is `/live`.
        A healthcheck on a 404 marks a healthy container unhealthy forever."""
        probe = re.search(r"HEALTHCHECK.*?curl[^|]*?(\S+)\"", dockerfile, re.S)
        assert probe, "healthcheck does not use curl"
        path = probe.group(1).strip('"')
        assert path.endswith("/live"), f"healthcheck probes {path}, not /live"

    def test_secrets_are_not_baked_in(self, dockerfile):
        for secret in ("sk-", "sk_live", "whsec_", "POSTGRES_PASSWORD="):
            assert secret not in dockerfile, f"{secret} appears in the Dockerfile"


class TestCompose:
    def test_defines_the_whole_stack(self, compose):
        assert set(compose["services"]) == {"db", "redis", "api", "worker"}

    def test_the_api_and_worker_share_one_image(self, compose):
        """Two services running different builds is how they drift apart."""
        for name in ("api", "worker"):
            assert compose["services"][name].get("build") == ".", name

    def test_services_wait_for_a_healthy_database(self, compose):
        """`depends_on` without a condition only waits for the container to
        start, not for Postgres to accept connections. The first migration
        would then fail against a database still initialising."""
        for name in ("api", "worker"):
            depends = compose["services"][name]["depends_on"]
            assert depends["db"]["condition"] == "service_healthy", name

    def test_postgres_password_has_no_default(self, compose):
        """A blank default boots a database reachable by anyone on the
        network, and it looks like a successful deploy."""
        env = compose["services"]["api"]["environment"]
        assert ":?" in env["DATABASE_URL"], "POSTGRES_PASSWORD is not required"

    def test_the_database_is_not_published_to_the_host(self, compose):
        assert "ports" not in compose["services"]["db"], "Postgres is exposed to the host"

    def test_worker_overrides_the_command(self, compose):
        """The worker must drain the queue, not serve HTTP."""
        assert compose["services"]["worker"]["command"] == ["python", "-m", "workers.worker"]

    def test_every_environment_key_is_a_real_setting(self, compose):
        """An env var the app does not read is either a typo or a setting that
        stopped having any effect. Both are worth failing on."""
        from app.core.config import Settings

        known = {name.upper() for name in Settings.model_fields}
        # Only the services that run our code. The db service's POSTGRES_*
        # variables are read by Postgres itself, not by SPARTON.
        for name in ("api", "worker"):
            for key in compose["services"][name].get("environment", {}):
                # Read by the container's own CMD, not by the app.
                if key.startswith("SPARTON_HOST") or key.startswith("SPARTON_PORT"):
                    continue
                assert key in known, f"{name}: {key} is not a Settings field"


class TestDockerignore:
    def test_secrets_and_local_data_are_excluded(self):
        """A .env or a SQLite file in a layer is a leak that survives a later
        `RUN rm`, because the bytes are still in the earlier layer."""
        text = DOCKERIGNORE.read_text(encoding="utf-8")
        for required in (".env", "*.db", "data/", ".git", "__pycache__/", "tests/"):
            assert required in text, f"{required} is not ignored"

    def test_env_example_is_not_ignored(self):
        """A blanket `.env*` rule would hide a genuine mistake during review."""
        assert "!.env.example" in DOCKERIGNORE.read_text(encoding="utf-8")


class TestEnvExample:
    def test_covers_the_settings_an_operator_must_set(self):
        """The variables that change behaviour in production, as opposed to the
        ones that have a safe default.

        A commented-out line still documents the variable, so both forms count.
        """
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for key in (
            "SPARTON_ENV", "APP_URL", "CORS_ORIGINS", "POSTGRES_PASSWORD",
            "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "STRIPE_PRICE_PRO",
            "STRIPE_PRICE_BUSINESS", "SMTP_HOST", "SMTP_FROM",
            "OPENAI_API_KEY", "LLM_PROVIDER", "BILLING_ENABLED", "REDIS_URL",
        ):
            assert re.search(rf"^#?\s*{key}=", text, re.M), f"{key} is undocumented"

    def test_contains_no_real_secret(self):
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        for pattern in (r"sk-[A-Za-z0-9]{20,}", r"whsec_[A-Za-z0-9]{20,}"):
            assert not re.search(pattern, text), "a real-looking secret is committed"
