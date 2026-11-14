"""Settings from environment / .env. Secrets never appear in code or logs."""

from __future__ import annotations

import hashlib
from zoneinfo import ZoneInfo

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

TASHKENT = ZoneInfo("Asia/Tashkent")
SPEC_VERSION = "2.0"  # HARI_HR_AI_Texnik_Topshiriq_UZ, 13.11.2026


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

    # Telegram IDs that are always owners (comma-separated). Set in Render → Environment; no shell needed.
    hari_owners: str = ""
    # "We reply within N working days" — told to applicants when they finish
    apply_reply_days: int = 3

    # ---- Spec 2.0 (13.11.2026) §25 "Бошланғич конфигурация". HARI never hires, rejects, disciplines or
    # changes pay by itself, trains on nothing and predicts nobody's leaving: those are not settings, the
    # code has no such path. What is left to set:
    #
    # shadow — the test stage (sample documents allowed; a missing value below is only reported).
    # live   — real staff data: a missing value blocks the workflow that needs it (§25).
    hari_mode: str = "shadow"
    # Work minutes without an answer before: one private reminder, the manager, the owner digest (§19).
    nonresponse_business_minutes: str = "120,240,480"
    # A message that Telegram refused for a while is tried this many times, then marked failed (§23).
    max_retries: int = 3
    # Lunch break left out of work minutes, "12-13"; empty = no break (§19 example).
    lunch_break: str = "12-13"
    # Saturday ends earlier than other work days (e.g. 14); empty = same hours as the other days.
    saturday_end_hour: int | None = None
    # Days nobody works (HR calendar): ISO dates, comma-separated.
    holidays: str = ""
    # Complaints about the owner or HR go to this verified staff account (Telegram ID) instead (§2).
    independent_complaint_recipient: int | None = None
    # AI requests allowed per day (the owner's approved budget, §23 "Чексиз харажат"). Empty = not approved.
    llm_daily_call_limit: int | None = None
    # Failures in a row before AI calls stop for a while, and for how long.
    llm_breaker_failures: int = 5
    llm_breaker_minutes: int = 10
    # Retention (§10): nothing is deleted by timer until a lawyer-approved policy version and days are set.
    retention_policy_version: str = ""
    retention_rejected_days: int | None = None
    # disabled — phones, e-mails, document numbers and the person's name are masked before any AI request.
    # allowed  — only after the lawyer accepts the AI provider (§10).
    external_personal_data_egress: str = "disabled"
    # Owner digest, Tashkent time "HH:MM" (§20). Weekly report: Friday at this time.
    owner_digest_time: str = "17:30"
    weekly_report_time: str = "16:00"
    # A group smaller than this is never broken out in a report (§20, T28).
    min_group_size: int = 5
    # Minutes an approval stays valid (§25 "Approval token").
    approval_ttl_minutes: int = 15

    @field_validator("saturday_end_hour", "independent_complaint_recipient", "llm_daily_call_limit",
                     "retention_rejected_days", mode="before")
    @classmethod
    def _empty_is_unset(cls, value):  # noqa: ANN001, ANN206 — an empty line in .env / Render means "not set"
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def is_live(self) -> bool:
        return self.hari_mode.strip().lower() == "live"

    @property
    def nonresponse_thresholds(self) -> tuple[int, int, int]:
        parts = [int(p) for p in (x.strip() for x in self.nonresponse_business_minutes.split(",")) if p.isdigit()]
        return (parts[0], parts[1], parts[2]) if len(parts) == 3 and parts[0] < parts[1] < parts[2] else (120, 240, 480)

    @property
    def lunch_hours(self) -> tuple[int, int] | None:
        start, _, end = self.lunch_break.partition("-")
        if start.strip().isdigit() and end.strip().isdigit() and 0 <= int(start) < int(end) <= 24:
            return int(start), int(end)
        return None

    @property
    def holiday_dates(self) -> frozenset[str]:
        return frozenset(x.strip() for x in self.holidays.split(",") if x.strip())

    def missing_config(self) -> list[str]:
        """Values §25 leaves empty on purpose: a person must fill them, never HARI."""
        missing = []
        if not self.owner_ids:
            missing.append("HARI_OWNERS")
        if self.independent_complaint_recipient is None:
            missing.append("INDEPENDENT_COMPLAINT_RECIPIENT")
        if self.llm_daily_call_limit is None:
            missing.append("LLM_DAILY_CALL_LIMIT")
        if not self.retention_policy_version or self.retention_rejected_days is None:
            missing.append("RETENTION_POLICY_VERSION")
        return missing

    @property
    def owner_ids(self) -> list[int]:
        return [int(p) for p in (x.strip() for x in self.hari_owners.split(",")) if p.isdigit()]

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
