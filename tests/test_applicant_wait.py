"""A submitted application nobody has decided on is reminded to owners and HR after one work day."""

from __future__ import annotations

from datetime import datetime, timezone

from hari import scheduler
from hari.config import TASHKENT
from hari.db.models import Application
from tests.conftest import FakeBot, make_staff

MONDAY, TUESDAY, WEDNESDAY = 9, 10, 11  # 2026-11-09 is a Monday


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TASHKENT)


def utc(dt: datetime) -> datetime:
    """What the app stores (SQLite drops the zone, and HARI reads stored times as UTC)."""
    return dt.astimezone(timezone.utc)


async def seed(session_scope, status: str = "submitted") -> None:
    async with session_scope() as s:
        await make_staff(s, "Владелец Тест", role="owner", tg=1001)
        await make_staff(s, "Кадр Тест", role="hr", tg=1002)
        await make_staff(s, "Ходим Тест", role="employee", tg=1003)  # ordinary staff are not reminded
        s.add(Application(id="app1", number="A-2026-11-0001", full_name="Алиев Вали", status=status,
                          submitted_at=utc(at(MONDAY, 10)), reported_at=utc(at(MONDAY, 10)), telegram_user_id=555))


async def test_reminds_owner_and_hr_after_one_work_day_then_daily(session_scope):
    await seed(session_scope)
    bot = FakeBot()
    await scheduler.tick(bot, at(MONDAY, 17))  # Monday evening: not yet a full work day
    assert bot.sent == []
    await scheduler.tick(bot, at(TUESDAY, 10, 30))  # Tuesday morning: due now
    assert sorted(chat for chat, _ in bot.sent) == [1001, 1002]  # owner and HR, not ordinary staff
    assert all("A-2026-11-0001" in text and "Алиев Вали" in text for _, text in bot.sent)
    await scheduler.tick(bot, at(TUESDAY, 15))  # same day again: no repeat
    assert len(bot.sent) == 2
    await scheduler.tick(bot, at(WEDNESDAY, 10, 30))  # next day: reminded again until decided
    assert len(bot.sent) == 4


async def test_decided_application_is_not_reminded(session_scope):
    await seed(session_scope, status="interview")
    bot = FakeBot()
    await scheduler.tick(bot, at(WEDNESDAY, 10, 30))
    assert bot.sent == []
