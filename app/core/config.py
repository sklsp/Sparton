"""SPARTON configuration.

One pydantic-settings object for the whole platform (Ares foundation,
extended with Apollo's local-creation settings). All settings are
environment driven (12-factor). Nothing secret is ever hardcoded here —
defaults are only provided for values that are safe to publish.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _resolve(value: str) -> str:
    """Resolve a possibly-relative configured path against the project root."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / value
    return str(path)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database -------------------------------------------------------
    # Postgres is the target database (docker compose provides one). The
    # SQLite default keeps `pytest` and a quick local run dependency free.
    database_url: str = "sqlite:///./sparton.db"
    db_echo: bool = False

    # --- LLM ------------------------------------------------------------
    llm_provider: str = "ollama"  # "ollama" | "openai_compatible" | "test"
    llm_timeout_seconds: float = 120.0

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"

    # --- Agent limits ---------------------------------------------------
    agent_max_iterations: int = 10
    agent_max_tool_calls: int = 24
    agent_timeout_seconds: int = 300
    agent_max_workers: int = 4
    # When true, POST /agent/run executes the agent inline instead of on a
    # background worker. Used by the test suite for deterministic runs.
    agent_run_inline: bool = False

    # --- Agent reliability ----------------------------------------------
    # Bounded retries for invalid tool arguments / failed tool calls.
    max_tool_retries: int = 2
    # Strict grounding: the agent may not state operational facts unless a
    # tool call actually succeeded. Default ON — fabricated data is worse
    # than an honest failure.
    strict_tool_grounding: bool = True

    # --- HTTP -----------------------------------------------------------
    cors_origins: str = (
        "http://localhost:3000,http://127.0.0.1:3000,https://sparton.vercel.app"
    )
    log_level: str = "INFO"
    # When set, every API request must present this value in X-API-Key.
    # Leave empty for open local development.
    api_key: str | None = None

    # --- Documents & RAG (Apollo) ----------------------------------------
    embedding_model: str = "nomic-embed-text"
    embedding_fallback_model: str = "all-MiniLM-L6-v2"
    rag_chunk_size: int = 800
    rag_chunk_overlap: int = 150
    rag_top_k: int = 4
    max_document_upload_mb: float = 25.0

    @property
    def max_document_upload_bytes(self) -> int:
        return int(self.max_document_upload_mb * 1024 * 1024)

    # --- Vision / captioning (Apollo) ------------------------------------
    vision_model: str = "llava"
    vision_timeout: float = 180.0

    # --- ComfyUI (Apollo) -------------------------------------------------
    comfyui_base_url: str = "http://127.0.0.1:8188"
    comfyui_timeout: float = 30.0
    comfyui_poll_interval: float = 1.0
    comfyui_generation_timeout: float = 600.0
    comfyui_workflow_dir: str = _resolve("./workflows")
    # ComfyUI's own models/loras folder, so manually added LoRAs are discoverable.
    comfyui_lora_dir: str = ""

    # --- Ostris AI Toolkit (external local process) -----------------------
    ai_toolkit_path: str = ""
    ai_toolkit_python: str = ""

    @property
    def ai_toolkit_configured(self) -> bool:
        """True only when both the toolkit path and its interpreter really exist."""
        return bool(
            self.ai_toolkit_path
            and self.ai_toolkit_python
            and Path(self.ai_toolkit_path, "run.py").is_file()
            and Path(self.ai_toolkit_python).is_file()
        )

    # --- Local storage roots (Apollo) --------------------------------------
    lora_data_dir: str = _resolve("./data/loras")
    generated_dir: str = _resolve("./data/generated")
    rag_dir: str = _resolve("./data/rag")
    documents_dir: str = _resolve("./data/documents")
    data_dir: str = _resolve("./data")

    # --- Upload limits -----------------------------------------------------
    max_image_upload_mb: float = 25.0

    @property
    def max_image_upload_bytes(self) -> int:
        return int(self.max_image_upload_mb * 1024 * 1024)

    # --- Intelligence crawling ----------------------------------------------
    # SSRF guard: private/loopback destinations are blocked unless this is
    # explicitly enabled for local development fixtures.
    crawler_allow_private_addresses: bool = False

    # --- Background jobs -----------------------------------------------------
    # Local development: the API runs an embedded worker. Production: run
    # `python -m app.worker` replicas and set EMBEDDED_WORKER=false here.
    embedded_worker: bool = True

    # --- Rate limiting ---------------------------------------------------------
    rate_limit_disabled: bool = False
    rate_limit_agent_per_minute: int = 10
    rate_limit_research_per_minute: int = 10
    rate_limit_login_per_minute: int = 20
    rate_limit_default_per_minute: int = 120
    # When set, rate limits are shared across API replicas via Redis.
    # Leave empty for single-process local development (in-process limiter).
    redis_url: str | None = None

    # --- Observability ------------------------------------------------------------
    # Tracing is optional: with OTEL_ENABLED=false the app runs identically
    # with no collector. Set OTEL_EXPORTER_OTLP_ENDPOINT in production.
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str | None = None
    otel_service_name: str = "sparton"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
