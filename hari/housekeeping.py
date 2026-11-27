"""Clearing what testing left in the live database, at start-up.

Requests opened by a card still named like the form's example ("Фамилия Исм Отасининг исми") come from
people trying HARI out, not from real employees. Left open, they come back in every morning message.
They are closed once, with a note, and the closing is logged; real requests are never touched. It does
nothing the second time.
"""

from __future__ import annotations

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit
from hari.db.models import CaseEvent, Employee, HRCase
from hari.translit import normalize
from hari.worktime import now_utc

PLACEHOLDER_NAMES = ("Фамилия Исм Отасининг исми", "Familiya Ism Otasining ismi", "Фамилия Имя Отчество",
                     "Фамилия Исм", "Familiya Ism")
NOTE = "Синов маълумоти: автоматик ёпилди."


def is_placeholder(full_name: str | None) -> bool:
    name = normalize((full_name or "").strip().lower())
    return any(name == normalize(p.lower()) for p in PLACEHOLDER_NAMES)


async def close_test_requests(session: AsyncSession) -> int:
    testers = [e.id for e in (await session.execute(select(Employee))).scalars() if is_placeholder(e.full_name)]
    if not testers:
        return 0
    open_ = (await session.execute(select(HRCase).where(
        HRCase.requester_id.in_(testers), HRCase.status.in_(("submitted", "accepted", "in_review", "decided"))
    ))).scalars().all()
    now = now_utc()
    for case in open_:
        case.status, case.closed_at = "closed", now
        case.decision = case.decision or NOTE
        session.add(CaseEvent(case_id=case.id, actor_id=None, event="closed"))
        await audit.record(session, "case.cleanup", "closed", object_type="case", object_id=case.number,
                           detail="test card")
    if open_:
        logger.info("Closed {} requests left by test cards", len(open_))
    return len(open_)
