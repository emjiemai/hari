"""Each person's own conversation with HARI, remembered across messages, days and restarts.

Like a chat with an assistant: HARI sees what this person wrote before and what it answered, so
"ha, yubor", "I told you yesterday" and "what about my previous question?" make sense.

Privacy, one person at a time:
- Every read and write is keyed by the employee the server resolved from the verified Telegram
  account of the sender. Nothing in a message or from the model can choose whose history is read.
- No screen shows one person's chat to anyone else, HR and the owner included. HR sees only the
  requests the person chose to open.
- A topic that gets a fixed safety reply (a complaint, a crisis, someone else's pay…) is not kept and
  never reaches the AI (hari.guard).
- Old exchanges are deleted after `chat_memory_days`; the person can erase their own at once with /unut.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hari.config import settings
from hari.db.models import ChatTurn, Employee, as_utc
from hari.worktime import local

MEMORY_EXCHANGES = 10  # earlier question/answer pairs the model sees with each new message
MAX_CHARS = 1500  # of each stored message


async def recent(session: AsyncSession, emp: Employee, now: datetime) -> list[tuple[str, str, str, datetime]]:
    """This person's last exchanges, oldest first: (their message, HARI's reply, Tashkent date/time, UTC time)."""
    since = now - timedelta(days=settings.chat_memory_days)
    rows = (await session.execute(
        select(ChatTurn).where(ChatTurn.employee_id == emp.id, ChatTurn.at >= since)
        .order_by(ChatTurn.at.desc()).limit(MEMORY_EXCHANGES))).scalars().all()
    return [(r.question, r.answer, local(as_utc(r.at)).strftime("%Y-%m-%d %H:%M"), as_utc(r.at))
            for r in reversed(rows)]


async def remember(session: AsyncSession, emp: Employee, question: str, answer: str, now: datetime) -> None:
    session.add(ChatTurn(employee_id=emp.id, at=now, question=question[:MAX_CHARS], answer=answer[:MAX_CHARS]))
    await session.flush()


async def forget(session: AsyncSession, emp: Employee) -> int:
    """The person's own request: erase everything HARI remembers of their chat."""
    result = await session.execute(delete(ChatTurn).where(ChatTurn.employee_id == emp.id))
    return result.rowcount or 0


async def purge_old(session: AsyncSession, now: datetime) -> int:
    result = await session.execute(
        delete(ChatTurn).where(ChatTurn.at < now - timedelta(days=settings.chat_memory_days)))
    return result.rowcount or 0
