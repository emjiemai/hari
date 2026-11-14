"""The outbox: every message HARI sends on its own is written here first, under an idempotency key,
then sent, then marked (spec §10, A09; spec 2.0 §23 "Такрорий хабар").

A key that was already used queues nothing, so the same event never produces a second message (T18).
Planned messages to one employee stop at PROACTIVE_DAILY_LIMIT a day; what did not fit is simply
not queued and is picked up at the next shift (T22). Answers to a person's own question and real
incidents are not counted.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hari.config import settings
from hari.db.models import Employee, OutboundMessage
from hari.worktime import today_local

# Counted against the daily limit: the morning onboarding plan and reminders about waiting work.
PROACTIVE_KINDS = {"onboarding", "reminder"}


async def proactive_sent_today(session: AsyncSession, emp: Employee, day) -> int:  # noqa: ANN001
    return (await session.execute(
        select(func.count()).select_from(OutboundMessage).where(
            OutboundMessage.employee_id == emp.id, OutboundMessage.local_date == day,
            OutboundMessage.kind.in_(PROACTIVE_KINDS), OutboundMessage.status != "failed")
    )).scalar_one()


async def queue(session: AsyncSession, emp: Employee, kind: str, key: str, text: str,
                now: datetime) -> OutboundMessage | None:
    """Reserve a message. None = already sent/queued under this key, or over the daily limit."""
    day = today_local(now)
    if (await session.execute(select(OutboundMessage.id).where(OutboundMessage.idempotency_key == key))).first():
        return None
    if kind in PROACTIVE_KINDS and await proactive_sent_today(session, emp, day) >= settings.proactive_daily_limit:
        return None
    msg = OutboundMessage(employee_id=emp.id, kind=kind, idempotency_key=key, local_date=day, text=text)
    session.add(msg)
    try:
        await session.flush()
    except IntegrityError:
        return None
    return msg
