"""Keeping and deleting applicants' personal data (spec 2.0 §10; T24).

- An applicant can ask for their data to be deleted. A person at HR decides; HARI does not delete on
  the request alone.
- A legal hold stops every deletion — by button or by timer — until the lawyer lifts it.
- Deleting by timer (rejected applicants after N days) runs only in live mode with a lawyer-approved
  policy version and a number of days set. Until then nothing is deleted by timer: the 6 months in the
  spec is a proposal, not a legal term, and HARI does not pick the number.
"Deleting" removes what identifies the person and what they wrote; the application number, the
vacancy, the dates and the decision with its written basis stay as the company's record.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit
from hari.config import settings
from hari.db.models import Application, Employee, as_utc
from hari.worktime import now_utc

REMOVED = "[ўчирилган]"


class RetentionError(ValueError):
    """Codes: legal_hold, already_purged."""


def purge(app: Application, now: datetime) -> None:
    app.full_name, app.phone, app.experience, app.note = REMOVED, "", "", ""
    app.resume_file_id, app.telegram_user_id = None, None
    app.job = [{"q": row.get("q", ""), "a": None} for row in (app.job or [])]
    app.logic, app.answer_notes, app.reason, app.scorecard = [], [], None, None
    if app.status in ("draft", "submitted", "interview"):
        app.status = "withdrawn"
    app.purged_at = now


async def request_deletion(session: AsyncSession, app: Application, now: datetime | None = None) -> bool:
    """The applicant's own request. Returns False if one is already waiting."""
    if app.deletion_requested_at is not None or app.purged_at is not None:
        return False
    app.deletion_requested_at = now or now_utc()
    await audit.record(session, "retention.request", "deletion_review", object_type="application", object_id=app.id)
    return True


async def set_legal_hold(session: AsyncSession, app: Application, actor: Employee, hold: bool) -> None:
    app.legal_hold = hold
    await audit.record(session, "retention.hold", "set" if hold else "lifted", actor_id=actor.id,
                       object_type="application", object_id=app.id)


async def delete_now(session: AsyncSession, app: Application, actor: Employee, now: datetime | None = None) -> None:
    """A person's decision to delete. A legal hold wins."""
    if app.purged_at is not None:
        raise RetentionError("already_purged")
    if app.legal_hold:
        await audit.record(session, "retention.delete", "blocked_legal_hold", actor_id=actor.id,
                           object_type="application", object_id=app.id)
        raise RetentionError("legal_hold")
    purge(app, now or now_utc())
    await audit.record(session, "retention.delete", "purged", actor_id=actor.id, object_type="application",
                       object_id=app.id)


def timer_allowed() -> bool:
    return settings.is_live and bool(settings.retention_policy_version) and settings.retention_rejected_days is not None


async def due(session: AsyncSession, now: datetime) -> list[Application]:
    """Rejected applications past the configured age, not on hold. Empty when no number of days is set."""
    days = settings.retention_rejected_days
    if days is None:
        return []
    rows = (await session.execute(
        select(Application).where(Application.status == "rejected", Application.purged_at.is_(None))
    )).scalars().all()
    limit = now - timedelta(days=days)
    return [a for a in rows if not a.legal_hold and a.decided_at and as_utc(a.decided_at) < limit]


async def run(session: AsyncSession, now: datetime) -> int:
    """The timer. Deletes only when `timer_allowed`; otherwise touches nothing."""
    if not timer_allowed():
        return 0
    rows = await due(session, now)
    for app in rows:
        purge(app, now)
        await audit.record(session, "retention.timer", "purged", object_type="application", object_id=app.id,
                           detail=f"policy={settings.retention_policy_version}")
    return len(rows)
