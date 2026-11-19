"""HARI knows what each person should do and tells them itself (hari.agenda): the morning brief for every
role, the next step after something is done, and the first steps the moment a newcomer joins."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from hari import cases as C
from hari import onboarding as O
from hari import scheduler
from hari.db.models import Application, Employee, OnboardingStep
from hari.employees import make_invite
from hari.worktime import today_local
from tests.conftest import FakeBot, at_tashkent
from tests.test_bot_flow import Harness

MONDAY, TUESDAY = date(2026, 11, 9), date(2026, 11, 10)
OWNER, HR, MENTOR, NEW, IDLE = 801, 802, 803, 804, 805


@pytest.fixture
async def team(session_scope, staff):
    async with session_scope() as s:
        await staff(s, "Эгаев Улуғ", "owner", department="Раҳбарият", position="Директор", tg=OWNER)
        await staff(s, "Кадрова Зайнаб", "hr", department="HR", position="HR масъули", tg=HR)
        mentor = await staff(s, "Устозов Анвар", position="Катта сотувчи", tg=MENTOR)
        new = await staff(s, "Янгиев Ботир", tg=NEW, mentor=mentor, start=MONDAY)
        await staff(s, "Бўш Ходим", tg=IDLE)
        await O.create_plan(s, new, MONDAY)
        plan = await O.get_plan(s, new.id)
        meeting = next(st for st in plan.steps if st.kind == "meeting")
        await O.submit(s, new, meeting.id, "Мураббий билан учрашдик", None)
        meeting.submitted_at = at_tashkent(MONDAY, 15)  # yesterday afternoon
        case, _ = await C.open_case(s, new, "certificate", "Банк учун маълумотнома")
        case.created_at = at_tashkent(MONDAY, 16)
        s.add(Application(id="ap1", number="A-2026-11-0001", full_name="Алиев Вали", status="submitted",
                          submitted_at=at_tashkent(MONDAY, 11), reported_at=at_tashkent(MONDAY, 11),
                          telegram_user_id=9999))
        return case.number


async def test_each_person_hears_what_they_should_do_this_morning(team):
    bot = FakeBot()
    await scheduler.tick(bot, at_tashkent(TUESDAY, 9, 5))
    got: dict[int, str] = {}
    for chat, text in bot.sent:
        if text.startswith("Хайрли тонг"):
            got[chat] = text

    newcomer = got[NEW]
    assert newcomer.startswith("Хайрли тонг, Ботир") and "Мослашув режангиздан" in newcomer
    assert "«1 ни ўқидим»" in newcomer and "Мураббийингиз билан танишув" not in newcomer  # sent: not theirs now

    mentor = got[MENTOR]
    assert "S1. Янгиев Ботир — «Мураббийингиз билан танишув»" in mentor and "«S1 ни қабул қил»" in mentor

    hr = got[HR]
    assert f"{team} — Маълумотнома керак" in hr and f"«{team} га жавоб: …»" in hr
    assert "A-2026-11-0001 — Алиев Вали" in hr and "«A-2026-11-0001 ни кўрсат»" in hr

    assert "A-2026-11-0001" in got[OWNER] and team not in got[OWNER]  # the owner is not the case's handler
    assert IDLE not in got  # nothing to do: no message at all
    assert not any("сизни кутяпти" in text for _, text in bot.sent)  # the brief already listed the application

    await scheduler.tick(bot, at_tashkent(TUESDAY, 9, 30))
    assert sum(text.startswith("Хайрли тонг") for _, text in bot.sent) == len(got)  # once a day


async def test_no_brief_in_the_afternoon_and_nothing_from_today(team, session_scope):
    bot = FakeBot()
    await scheduler.tick(bot, at_tashkent(TUESDAY, 15))  # the morning has passed
    assert not any(text.startswith("Хайрли тонг") for _, text in bot.sent)
    bot = FakeBot()
    await scheduler.tick(bot, at_tashkent(MONDAY, 9, 5))  # Monday: the result and request arrive later that day
    briefs = {chat: text for chat, text in bot.sent if text.startswith("Хайрли тонг")}
    assert "S1." not in briefs.get(MENTOR, "") and "HR-" not in briefs.get(HR, "")


async def test_mentor_writes_s1_and_the_newcomer_hears_the_next_step(team, session_scope):
    h = Harness()
    replies = await h.say(MENTOR, "S1 ни қабул қил")
    assert "Тасдиқлайсизми?" in replies[0] and "Мураббийингиз билан танишув" in replies[0]
    await h.say(MENTOR, "ҳа")
    told = h.to(NEW)[-1]
    assert "қабул қилинди" in told and "Кейинги қадам: 1." in told and "«1 ни ўқидим»" in told


async def test_reading_a_step_points_to_the_next_one(team):
    h = Harness()
    replies = await h.say(NEW, "1 ни ўқидим")
    assert "танишганингиз қайд этилди" in replies[0] and "Кейинги қадам: 2." in replies[0]


async def test_a_newcomer_who_joins_is_told_where_to_start_and_the_mentor_is_told_too(session_scope, staff):
    from hari.bot import common

    common.bot_username = "mgmHaribot"
    async with session_scope() as s:
        mentor = await staff(s, "Устозов Анвар", position="Катта сотувчи", tg=MENTOR)
        day = today_local()
        while day.weekday() >= 5:  # a work day, so the first-day steps are due
            day -= timedelta(days=1)
        new = await staff(s, "Янгиев Ботир", position="Сотувчи", mentor=mentor, start=day)
        await O.create_plan(s, new, day)
        token = await make_invite(s, new, created_by=None)
    h = Harness()
    replies = await h.say(NEW, f"/start {token}")
    assert "Хуш келибсиз" in replies[0] and replies[1].startswith("Бугун шундан бошлаймиз")
    assert "«1 ни очинг»" in replies[1]
    assert "Янгиев Ботир (Сотувчи) HARI'га қўшилди, сиз унинг мураббийисиз" in h.to(MENTOR)[-1]
    async with session_scope() as s:
        assert (await s.execute(select(Employee).where(Employee.telegram_user_id == NEW))).scalar_one()
        assert (await s.execute(select(OnboardingStep))).scalars().first() is not None
