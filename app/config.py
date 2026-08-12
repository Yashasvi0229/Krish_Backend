"""
Application settings.

All environment variables are loaded and validated here via pydantic-settings.
Import `settings` from this module everywhere else — never read os.environ directly.

Docs: https://docs.pydantic.dev/latest/concepts/pydantic_settings/
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central configuration object. Immutable after first read."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",   # ignore unknown env vars instead of erroring
    )

    # ---- App ----
    app_env: Literal["development", "staging", "production"] = "development"
    app_name: str = "GNC Invoice Automation"
    app_version: str = "1.0.0"
    debug: bool = True
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    timezone: str = "America/Edmonton"

    # ---- Server ----
    backend_host: str = "0.0.0.0"
    backend_port: int = 8000
    frontend_url: str = "http://localhost:5173"
    allowed_origins: str = "http://localhost:5173,http://localhost:3000"

    # ---- Database ----
    database_url: str = Field(
        default="postgresql+asyncpg://gnc:gnc_pass@localhost:5432/gnc_invoice",
        description="Async DB URL used by the app at runtime.",
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg2://gnc:gnc_pass@localhost:5432/gnc_invoice",
        description="Sync DB URL used by Alembic migrations only.",
    )
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_echo: bool = False

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"
    redis_cache_db: int = 1

    # ---- Celery ----
    celery_broker_url: str = "redis://localhost:6379/2"
    celery_result_backend: str = "redis://localhost:6379/3"
    celery_worker_concurrency: int = 4
    celery_task_time_limit: int = 600
    celery_task_soft_time_limit: int = 540

    # ---- Security ----
    secret_key: str = "change-me-in-production"
    key_encryption_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_hours: int = 24

    # ---- Admin credentials (Phase 1 single-admin login — Step 3) ----
    # Later replaced by real users table. For now, one hardcoded admin per env.
    admin_email: str = "admin@gnc.local"
    admin_password: str = ""      # empty = login disabled (prod must set this)
    admin_display_name: str = "Administrator"

    # ---- Google OAuth ----
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/auth/google/callback"
    google_allowed_domain: str = ""   # empty = allow any Gmail account

    # ---- AI providers (see AI (Step 5) block below for the live settings) ----
    ai_default_provider: Literal["claude", "openai", "gemini"] = "openai"
    claude_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # ---- File Storage ----
    storage_root: str = "./storage"
    max_attachment_size_mb: int = 20

    # ---- OCR ----
    tesseract_cmd: str = "/usr/bin/tesseract"
    tesseract_lang: str = "eng"
    ocr_timeout_seconds: int = 60
    ocr_min_chars_before_ocr: int = 50   # if extract yields fewer chars, try OCR

    # ---- Gmail ----
    gmail_max_results_per_search: int = 500
    gmail_batch_size: int = 50
    gmail_internal_domain: str = "gncgroup.ca"   # emails from/to this → is_internal=true
    gmail_sync_lookback_days: int = 30           # /sync-recent default window

    # ---- Celery ----
    celery_task_always_eager: bool = False       # true = run tasks inline (free tier)

    # ---- AI (Step 5) ----
    # Provider — currently only "openai" is implemented but the config accepts
    # future providers so a switch requires an env change, not a code change.
    ai_provider: str = "openai"

    # OpenAI API key. Leave empty locally to run tests without spending money —
    # the AI service will refuse to make live calls and return a stubbed
    # analysis instead. On Render, set this to your sk-... key.
    openai_api_key: str = ""

    # Model selection. gpt-4o-mini is a good default: cheap ($0.15/1M in, $0.60/1M out),
    # supports JSON schema structured output, ~128K context.
    # Switch to gpt-4o for higher-accuracy runs (~15x more expensive).
    ai_model_primary: str = "gpt-4o-mini"
    ai_model_fallback: str = "gpt-4o"    # used when confidence is very low

    # Cost/latency guards — hard caps so no single email can blow the budget.
    ai_max_input_tokens: int = 60000     # ~200 KB of text; caller must truncate
    ai_max_output_tokens: int = 2000     # our JSON output is well under 500
    ai_request_timeout_seconds: int = 60
    ai_max_retries: int = 3

    # Prompt version — bump when we change the prompt template. Cached analyses
    # from earlier versions are automatically ignored (they don't match the
    # `input_hash` which factors in the prompt_version). Never manually
    # invalidate the cache; bumping this string does it.
    ai_prompt_version: str = "v1.0"

    # Confidence threshold — analyses below this get `requires_manual_review`.
    ai_manual_review_threshold: int = 70   # 0-100 scale

    # ---- Business rules ----
    max_hours_per_line_item: float = 3.9
    site_visit_hour_cap: float = 4.0
    default_currency: str = "CAD"
    default_gst_percent: float = 0.0

    # ---- Monitoring ----
    sentry_dsn: str = ""
    sentry_environment: str = "development"

    # ---- Derived helpers ----
    @property
    def allowed_origins_list(self) -> list[str]:
        """CORS origins as a list — parsed from comma-separated env var."""
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    @property
    def storage_root_path(self) -> Path:
        """Storage root as an absolute Path object. Created on first access."""
        p = Path(self.storage_root).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    # ---- Validators ----
    @field_validator("database_url", mode="before")
    @classmethod
    def _ensure_async_driver(cls, v: str) -> str:
        """
        Render (and Heroku) expose Postgres as `postgresql://...`.
        SQLAlchemy needs an explicit async driver — rewrite to `postgresql+asyncpg://`.
        Also strip any `?sslmode=` query param since asyncpg doesn't understand it
        (asyncpg uses `ssl=True` instead, which we set via connect_args if needed).
        """
        if not v:
            return v
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+asyncpg://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://", 1)
        # asyncpg doesn't accept sslmode as a query param — remove it if present.
        if "?" in v:
            base, _, query = v.partition("?")
            kept = "&".join(
                p for p in query.split("&") if not p.startswith("sslmode=")
            )
            v = f"{base}?{kept}" if kept else base
        return v

    @field_validator("database_url_sync", mode="before")
    @classmethod
    def _ensure_sync_driver(cls, v: str) -> str:
        """Same idea for Alembic — needs `postgresql+psycopg2://`."""
        if not v:
            return v
        if v.startswith("postgres://"):
            v = v.replace("postgres://", "postgresql+psycopg2://", 1)
        elif v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+psycopg2://", 1)
        return v

    @field_validator("secret_key", "key_encryption_key")
    @classmethod
    def _reject_empty_secrets(cls, v: str, info) -> str:
        if not v:
            raise ValueError(f"{info.field_name} must not be empty")
        return v


@lru_cache
def get_settings() -> Settings:
    """
    Cached settings accessor. Import this and call it, OR import the
    module-level `settings` singleton below — both work.
    """
    return Settings()


# Module-level singleton — most code should just `from app.config import settings`
settings = get_settings()
