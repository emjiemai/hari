"""Spending guard for AI requests (spec 2.0 §23 "Чексиз харажат", T23).

Two brakes, both in this process's memory:
- a daily limit of requests, set by the owner (LLM_DAILY_CALL_LIMIT). Reached → no more AI requests
  that Tashkent day; HARI keeps answering with the approved excerpt (A12).
- a circuit breaker: several failures in a row → no requests for a few minutes, instead of hammering
  a provider that is down.
In live mode a limit that was never set blocks AI requests: nobody approved a budget (§25, §27).
The counter restarts with the process; a restart can at most allow one more day's worth.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from hari.config import settings
from hari.worktime import now_utc, today_local


class BudgetState:
    def __init__(self) -> None:
        self.day: date | None = None
        self.calls = 0
        self.failures = 0
        self.open_until: datetime | None = None
        self.tripped_on: date | None = None  # the day the limit was first hit, for the IT signal

    def _roll(self, now: datetime) -> None:
        today = today_local(now)
        if self.day != today:
            self.day, self.calls = today, 0

    def refusal(self, now: datetime | None = None) -> str | None:
        """Why an AI request may not go out right now, or None."""
        now = now or now_utc()
        self._roll(now)
        limit = settings.llm_daily_call_limit
        if limit is None:
            return "budget_not_approved" if settings.is_live else None
        if self.calls >= limit:
            self.tripped_on = self.day
            return "daily_limit"
        if self.open_until is not None and now < self.open_until:
            return "circuit_open"
        return None

    def spent(self, now: datetime | None = None) -> None:
        self._roll(now or now_utc())
        self.calls += 1

    def succeeded(self) -> None:
        self.failures, self.open_until = 0, None

    def failed(self, now: datetime | None = None) -> None:
        self.failures += 1
        if self.failures >= settings.llm_breaker_failures:
            self.open_until = (now or now_utc()) + timedelta(minutes=settings.llm_breaker_minutes)
            self.failures = 0

    def status(self, now: datetime | None = None) -> dict:
        now = now or now_utc()
        self._roll(now)
        return {"calls_today": self.calls, "limit": settings.llm_daily_call_limit,
                "limit_hit_today": self.tripped_on == self.day,
                "circuit_open": self.open_until is not None and now < self.open_until}


state = BudgetState()


def reset() -> None:
    """Tests start from zero."""
    global state
    state = BudgetState()
