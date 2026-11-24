"""Giving a colleague work by writing: "Анвар backend'ни қилсин" → one «ҳа» → Анвар is told; when Анвар
says it is done, whoever gave it is told."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from hari import intent as I
from hari.db import session as db
from hari.db.models import Task
from tests.test_bot_flow import HR, MENTOR, Harness, no_buttons
from tests.test_intent import use_router

OWNER, BOSS = 600, 601


@pytest.fixture
async def h(session_scope, staff):
    from hari.bot import common

    common.bot_username = "mgmHaribot"
    async with session_scope() as s:
        await staff(s, "Эгаси Шахзод", "owner", department="Раҳбарият", position="Директор", tg=OWNER)  # E-0001
        await staff(s, "Кадрова Зайнаб", "hr", department="HR", position="HR масъули", tg=HR)  # E-0002
        await staff(s, "Устозов Анвар", position="Дастурчи", tg=MENTOR)  # E-0003
        await staff(s, "Бошлиқов Карим", "manager", position="Бўлим бошлиғи", tg=BOSS)  # E-0004
    return Harness()


async def _tasks() -> list[Task]:
    async with db.session_scope() as s:
        return list((await s.execute(select(Task))).scalars())


async def test_owner_gives_work_and_the_person_is_told(h, monkeypatch):
    router = use_router(monkeypatch, {"action": "assign", "args": {"who": "E-0003", "text": "backend'ни қилсин",
                                                                   "due": None}})
    await h.say(OWNER, "Анвар backend'ни қилсин")
    assert "Анвар" not in router.prompts[0].split("MESSAGE:")[1]  # the AI saw a code, not the name
    told = h.to(MENTOR)[-1]  # a small step: sent at once, no «ҳа»
    assert "Эгаси Шахзоддан топшириқ" in told and "«backend'ни қилсин»" in told
    assert "Устозов Анвар" in h.to(OWNER)[-1] and "кечқурун сизга айтаман" in h.to(OWNER)[-1]
    (task,) = await _tasks()
    assert task.status == "open" and task.text == "backend'ни қилсин" and task.due is None

    router = use_router(monkeypatch, {"action": "task_done", "args": {"task": "T1", "text": None}})
    await h.say(MENTOR, "backend'ни бажардим")
    assert "my_tasks" in router.prompts[0] and "T1" in router.prompts[0]
    assert "Эгаси Шахзодга айтдим" in h.to(MENTOR)[-1]
    assert "Устозов Анвар топшириқни бажарди: «backend'ни қилсин»" in h.to(OWNER)[-1]
    (task,) = await _tasks()
    assert task.status == "done" and task.done_at is not None
    no_buttons(h)


async def test_a_due_date_is_kept_and_told(h, monkeypatch):
    from datetime import timedelta

    from hari.worktime import today_local

    friday = today_local() + timedelta(days=3)
    use_router(monkeypatch, {"action": "assign", "args": {"who": "E-0003", "text": "ҳисоботни топширсин",
                                                          "due": friday.isoformat()}})
    await h.say(OWNER, "Анвар жумагача ҳисоботни топширсин")
    assert f"Муддат: {friday:%d.%m.%Y}" in h.to(MENTOR)[-1]
    (task,) = await _tasks()
    assert task.due == friday
    use_router(monkeypatch, {"action": "assign", "args": {"who": "E-0003", "text": "эски иш", "due": "2020-01-01"}})
    await h.say(OWNER, "Анвар эски иш қилсин")
    assert [t.due for t in await _tasks() if t.text == "эски иш"] == [None]  # a day in the past is not a due date


async def test_the_work_is_taken_from_the_message_when_the_ai_leaves_it_out(h, monkeypatch):
    use_router(monkeypatch, {"action": "assign", "args": {"who": "E-0003", "text": None}})
    await h.say(BOSS, "Анварга айт, мижозга қўнғироқ қилсин")
    assert "«мижозга қўнғироқ қилсин»" in h.to(MENTOR)[-1]


async def test_no_work_named_hari_asks_what(h, monkeypatch):
    use_router(monkeypatch, {"action": "assign", "args": {"who": "E-0003", "text": None}})
    replies = await h.say(HR, "Анвар")
    assert "Анвар нима қилиши керак?" in replies[0]
    await h.say(HR, "Ҳисоботни жумагача топширсин")
    assert "«Ҳисоботни жумагача топширсин»" in h.to(MENTOR)[-1]


async def test_both_sides_see_their_tasks(h, monkeypatch):
    use_router(monkeypatch, {"action": "assign", "args": {"who": "E-0003", "text": "backend'ни қилсин"}})
    await h.say(OWNER, "Анвар backend'ни қилсин")
    mine = (await h.say(MENTOR, "топшириқларим"))[0]
    assert "Сизга берилган" in mine and "backend'ни қилсин" in mine and "Эгаси Шахзод" in mine
    given = (await h.say(OWNER, "/topshiriqlar"))[0]
    assert "Сиз берганлар" in given and "Устозов Анвар" in given and "ҳали бажарилмади" in given


def test_who_may_give_work():
    assert I.allowed("assign", "owner", False, True) and I.allowed("assign", "hr", False, True)
    assert I.allowed("assign", "manager", True, False)
    assert not I.allowed("assign", "employee", True, False)  # a mentor gives steps, not orders
    assert I.allowed("task_done", "employee", False, False) and I.allowed("tasks", "employee", False, False)


# --------------------------------------------------------------------------- how is it going, and 20:00

async def _give(session_scope, at, text="backend'ни қилсин") -> str:
    from hari.employees import by_telegram

    async with session_scope() as s:
        task = Task(from_id=(await by_telegram(s, OWNER)).id, to_id=(await by_telegram(s, MENTOR)).id, text=text, at=at)
        s.add(task)
        await s.flush()
        return task.id


async def test_hari_asks_how_it_is_going_once_a_day_in_work_hours(h, session_scope):
    from datetime import date

    from hari import taskwatch
    from hari.db.models import OutboundMessage
    from tests.conftest import at_tashkent

    monday = date(2026, 11, 9)
    await _give(session_scope, at_tashkent(monday, 9))
    async with session_scope() as s:
        assert await taskwatch.check_ins(s, at_tashkent(monday, 11)) == []  # too soon
        assert len(await taskwatch.check_ins(s, at_tashkent(monday, 12, 30))) == 1
        assert await taskwatch.check_ins(s, at_tashkent(monday, 16)) == []  # once a day
        assert await taskwatch.check_ins(s, at_tashkent(date(2026, 11, 10), 7)) == []  # before their hours
        assert len(await taskwatch.check_ins(s, at_tashkent(date(2026, 11, 10), 10))) == 1  # the next day
        msg = (await s.execute(select(OutboundMessage).where(OutboundMessage.kind == "task_check"))).scalars().first()
        assert "«backend'ни қилсин» топшириғи қандай кетяпти?" in msg.text and "Эгаси Шахзод" in msg.text


async def test_the_answer_is_kept_with_ai_and_without(h, session_scope, monkeypatch):
    from datetime import date

    from hari import taskwatch
    from hari.db.models import TaskUpdate
    from tests.conftest import at_tashkent

    await _give(session_scope, at_tashkent(date(2026, 11, 9), 9))
    async with session_scope() as s:
        await taskwatch.check_ins(s, at_tashkent(date(2026, 11, 9), 13))
    router = use_router(monkeypatch, {"action": "task_status", "args": {"task": "T1", "text": "ярмини қилдим"}})
    await h.say(MENTOR, "ярмини қилдим")
    assert "hari_asked_how_it_is_going" in router.prompts[0]
    assert "Ёзиб қўйдим. Эгаси Шахзодга кечқурун айтаман." in h.to(MENTOR)[-1]
    assert not any("ярмини" in t for t in h.to(OWNER))  # not now: in the evening

    from datetime import timedelta

    from hari.worktime import now_utc

    async with session_scope() as s:  # HARI asks again later
        (task,) = (await s.execute(select(Task))).scalars()
        task.asked_at = now_utc() + timedelta(seconds=1)  # Windows clock steps are ~16 ms
    use_router(monkeypatch)  # the AI is down: the answer to HARI's question is still kept
    await h.say(MENTOR, "API тайёр, эртага тестлайман")
    async with db.session_scope() as s:
        texts = [u.text for u in (await s.execute(select(TaskUpdate).order_by(TaskUpdate.at))).scalars()]
    assert texts == ["ярмини қилдим", "API тайёр, эртага тестлайман"]


async def test_the_giver_hears_at_eight_in_the_evening(h, session_scope):
    from datetime import date

    from hari import taskwatch
    from hari.db.models import OutboundMessage, TaskUpdate
    from tests.conftest import at_tashkent

    monday = date(2026, 11, 9)
    first = await _give(session_scope, at_tashkent(monday, 9))
    second = await _give(session_scope, at_tashkent(monday, 9), "дизайнни тузат")
    third = await _give(session_scope, at_tashkent(monday, 9), "мижозга қўнғироқ қил")
    async with session_scope() as s:
        s.add(TaskUpdate(task_id=first, text="ярмини қилдим", at=at_tashkent(monday, 15)))
        done = await s.get(Task, third)
        done.status, done.done_at = "done", at_tashkent(monday, 16)
    async with session_scope() as s:
        assert await taskwatch.evening(s, at_tashkent(monday, 19, 59)) == []
        assert len(await taskwatch.evening(s, at_tashkent(monday, 20))) == 1
        assert await taskwatch.evening(s, at_tashkent(monday, 21)) == []  # one report a day
        msg = (await s.execute(select(OutboundMessage).where(OutboundMessage.kind == "task_report"))).scalar_one()
    assert "Устозов Анвар: «backend'ни қилсин» — ярмини қилдим (15:00)" in msg.text
    assert "«дизайнни тузат» — ҳали ҳеч нарса ёзмади" in msg.text
    assert "«мижозга қўнғироқ қил» — бажарилди (16:00)" in msg.text
    assert second


async def test_a_late_task_is_told_to_the_giver_once(h, session_scope):
    from datetime import date

    from hari import taskwatch
    from hari.db.models import OutboundMessage
    from tests.conftest import at_tashkent

    monday, tuesday = date(2026, 11, 9), date(2026, 11, 10)
    task_id = await _give(session_scope, at_tashkent(monday, 9))
    async with session_scope() as s:
        (await s.get(Task, task_id)).due = monday
    async with session_scope() as s:
        assert len(await taskwatch.check_ins(s, at_tashkent(monday, 10))) == 1  # the due day: asked at once
        msg = (await s.execute(select(OutboundMessage).where(OutboundMessage.kind == "task_check"))).scalar_one()
        assert "Бугун «backend'ни қилсин» топшириғининг муддати" in msg.text
        assert await taskwatch.overdue(s, at_tashkent(monday, 17)) == []  # not late yet
        assert len(await taskwatch.overdue(s, at_tashkent(tuesday, 10))) == 1
        assert await taskwatch.overdue(s, at_tashkent(tuesday, 15)) == []  # once
        late = (await s.execute(select(OutboundMessage).where(OutboundMessage.kind == "task_overdue"))).scalar_one()
    assert "Устозов Анвар: «backend'ни қилсин» — муддати 09.11 эди, ҳали бажарилмади" in late.text
