"""Timers: work hours, daily limit, no duplicates after restart, escalation (spec §10; A07, A09)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from hari import cases as C
from hari import onboarding as O
from hari import scheduler
from hari.config import settings
from hari.db.models import OutboundMessage
from tests.conftest import FakeBot, at_tashkent

MONDAY = date(2026, 11, 9)


@pytest.fixture
async def newcomer(session_scope, staff):
    async with session_scope() as s:
        emp = await staff(s, "Янгиев Ботир Акмалович", tg=14, start=MONDAY)
        await O.create_plan(s, emp, MONDAY)
        return emp


async def test_a07_no_nudge_outside_work_hours_or_on_day_off(newcomer):
    bot = FakeBot()
    assert await scheduler.tick(bot, at_tashkent(MONDAY, 7)) == 0  # before 09:00
    assert await scheduler.tick(bot, at_tashkent(MONDAY, 19)) == 0  # after 18:00
    assert await scheduler.tick(bot, at_tashkent(date(2026, 11, 8), 10)) == 0  # Sunday
    assert bot.sent == []


async def test_one_nudge_per_day_even_with_many_ticks(newcomer):
    bot = FakeBot()
    for minute in range(0, 50, 10):
        await scheduler.tick(bot, at_tashkent(MONDAY, 9, minute))
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 14 and "Ботир" in text and "Режам" in text
    await scheduler.tick(bot, at_tashkent(date(2026, 11, 10), 9, 5))
    assert len(bot.sent) == 2  # next work day: a new one


async def test_absent_employee_gets_no_nudge(newcomer, session_scope):
    from hari.db.models import Employee

    async with session_scope() as s:
        (await s.get(Employee, newcomer.id)).status = "absent"
    bot = FakeBot()
    await scheduler.tick(bot, at_tashkent(MONDAY, 10))
    assert bot.sent == []


async def test_daily_limit_blocks_extra_proactive_messages(newcomer, session_scope, monkeypatch):
    monkeypatch.setattr(settings, "proactive_daily_limit", 1)
    async with session_scope() as s:
        from hari.db.models import Employee

        emp = await s.get(Employee, newcomer.id)
        now = at_tashkent(MONDAY, 10)
        assert await scheduler.queue(s, emp, "onboarding", "k1", "a", now) is not None
        assert await scheduler.queue(s, emp, "onboarding", "k2", "b", now) is None  # over the limit
        assert await scheduler.queue(s, emp, "escalation", "k3", "c", now) is not None  # not counted


async def test_a09_failed_send_is_retried_later_not_lost_and_not_doubled(newcomer, session_scope):
    down = FakeBot(fail=True)
    assert await scheduler.tick(down, at_tashkent(MONDAY, 9, 0)) == 0
    async with session_scope() as s:
        rows = (await s.execute(select(OutboundMessage))).scalars().all()
        assert len(rows) == 1 and rows[0].status == "pending"
        rows[0].created_at = at_tashkent(MONDAY, 8, 50)  # make it old enough to retry
    up = FakeBot()
    await scheduler.tick(up, at_tashkent(MONDAY, 9, 10))
    await scheduler.tick(up, at_tashkent(MONDAY, 9, 20))
    assert len(up.sent) == 1
    async with session_scope() as s:
        assert (await s.execute(select(OutboundMessage))).scalar_one().status == "sent"


async def test_unanswered_case_goes_reminder_then_manager_then_owner_digest(session_scope, staff):
    """Spec 2.0 §19: 120 work minutes → one private reminder, 240 → the manager, 480 → the owner's digest."""
    from hari import nonresponse

    async with session_scope() as s:
        boss = await staff(s, "Эгаев Улуғ", "owner", tg=10)
        await staff(s, "Кадрова Зайнаб", "hr", tg=11, manager=boss)
        emp = await staff(s, "Аълоев Анвар", tg=12)
        case, _ = await C.open_case(s, emp, "certificate", "маълумотнома")
        hr = await s.get(type(emp), case.assignee_id)
        await nonresponse.track(s, hr, "case", case.id, nonresponse.case_title(case), delivered=True,
                                now=at_tashkent(MONDAY, 9))
    bot = FakeBot()
    await scheduler.tick(bot, at_tashkent(MONDAY, 10, 30))  # 90 work minutes
    assert bot.sent == []
    await scheduler.tick(bot, at_tashkent(MONDAY, 11, 5))  # 125
    await scheduler.tick(bot, at_tashkent(MONDAY, 11, 30))  # still one reminder
    assert [chat for chat, _ in bot.sent] == [11] and "Жавобингиз кутилмоқда" in bot.sent[0][1]
    await scheduler.tick(bot, at_tashkent(MONDAY, 14, 5))  # 9–12 and 13–14:05 = 245 (lunch is not counted)
    assert [chat for chat, _ in bot.sent] == [11, 10] and "хулоса эмас" in bot.sent[1][1]
    await scheduler.tick(bot, at_tashkent(MONDAY, 17, 45))  # 465: not yet 480, and no digest line
    assert len(bot.sent) == 2
    await scheduler.tick(bot, at_tashkent(date(2026, 11, 10), 9, 20))  # 480 + 20, next morning
    await scheduler.tick(bot, at_tashkent(date(2026, 11, 10), 17, 35))  # the daily digest
    assert [chat for chat, _ in bot.sent] == [11, 10, 10]
    assert "кунлик дайжест" in bot.sent[2][1] and "HR-" in bot.sent[2][1] and "жавобсиз" in bot.sent[2][1]


async def test_answering_the_case_stops_the_reminders(session_scope, staff):
    from hari import nonresponse

    async with session_scope() as s:
        await staff(s, "Эгаев Улуғ", "owner", tg=10)
        hr = await staff(s, "Кадрова Зайнаб", "hr", tg=11)
        emp = await staff(s, "Аълоев Анвар", tg=12)
        case, _ = await C.open_case(s, emp, "certificate", "маълумотнома")
        await nonresponse.track(s, hr, "case", case.id, nonresponse.case_title(case), delivered=True,
                                now=at_tashkent(MONDAY, 9))
        await C.take(s, hr, case)
    bot = FakeBot()
    await scheduler.tick(bot, at_tashkent(MONDAY, 17))
    assert bot.sent == []


async def test_paused_scheduler_sends_nothing(newcomer, monkeypatch):
    monkeypatch.setattr(settings, "hari_paused", True)
    bot = FakeBot()
    assert await scheduler.tick(bot, at_tashkent(MONDAY, 10)) == 0 and bot.sent == []
