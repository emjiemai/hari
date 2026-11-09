"""Settings from environment / .env. Secrets never appear in code or logs."""

from __future__ import annotations

import hashlib
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

TASHKENT = ZoneInfo("Asia/Tashkent")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr = SecretStr("")
    database_url: str = "sqlite+aiosqlite:///./hari.db"

    openrouter_api_key: SecretStr = SecretStr("")
    llm_model: str = "google/gemini-3.8-flash"
    llm_fallback_models: str = "google/gemini-3.7-flash"
    llm_timeout_seconds: float = 40.0

    knowledge_dir: str = "knowledge/samples"
    proactive_daily_limit: int = 2
    hari_paused: bool = False

    # Work hours used for proactive messages when an employee has no schedule (spec §10, A07)
    default_work_start_hour: int = 9
    default_work_end_hour: int = 18
    # First answer target for an HR case, in work hours (spec §10 escalation)
    case_first_response_hours: int = 4
    # One-time invite link lifetime
    invite_ttl_hours: int = 72

    # How Telegram reaches HARI.
    #   polling — HARI asks Telegram for updates (local PC, no public address needed)
    #   webhook — Telegram calls HARI's public HTTPS address (Render web service)
    bot_mode: str = "polling"
    # Public base address for the webhook. On Render it comes by itself from RENDER_EXTERNAL_URL.
    webhook_base_url: str = ""
    render_external_url: str = ""
    # Shared secret Telegram sends back in a header; empty = derived from the bot token.
    webhook_secret: SecretStr = SecretStr("")
    port: int = 8080
    # Log file next to stdout. Empty = stdout only (Render keeps stdout logs; its disk is temporary).
    log_file: str = "logs/hari.log"

    @property
    def llm_models(self) -> list[str]:
        extra = [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]
        return [self.llm_model, *extra]

    @property
    def webhook_url(self) -> str:
        base = (self.webhook_base_url or self.render_external_url).rstrip("/")
        return f"{base}{WEBHOOK_PATH}" if base else ""

    @property
    def telegram_secret_token(self) -> str:
        """Telegram allows only A–Z, a–z, 0–9, _ and - here, so a hash of the secret is sent, never the secret."""
        source = self.webhook_secret.get_secret_value() or self.telegram_bot_token.get_secret_value()
        return hashlib.sha256(f"hari-webhook:{source}".encode("utf-8")).hexdigest()

    @property
    def async_database_url(self) -> str:
        return normalize_database_url(self.database_url)[0]


WEBHOOK_PATH = "/telegram/webhook"


def normalize_database_url(url: str) -> tuple[str, dict]:
    """Render gives `postgres://…` or `postgresql://…?sslmode=require`; SQLAlchemy's async driver
    needs `postgresql+asyncpg://…` and takes SSL as a connect argument, not `sslmode`."""
    connect_args: dict = {}
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    if url.startswith("postgresql+asyncpg://") and "?" in url:
        base, _, query = url.partition("?")
        kept = []
        for part in query.split("&"):
            key, _, value = part.partition("=")
            if key in ("sslmode", "ssl"):
                if value and value not in ("disable", "allow"):
                    connect_args["ssl"] = "require"
            elif part:
                kept.append(part)
        url = base + ("?" + "&".join(kept) if kept else "")
    return url, connect_args


settings = Settings()
