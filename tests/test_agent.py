"""HARI as an agent (hari.agent): one message → it looks things up → several actions, or an answer from
the data. Small steps are just done; risky ones wait for one «ҳа» together. Names never reach the AI."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from hari.db import session as db
from hari.db.models import Employee, Task, TaskUpdate
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


async def _role(tg: int) -> str:
    async with db.session_scope() as s:
        return (await s.execute(select(Employee.role).where(Employee.telegram_user_id == tg))).scalar_one()


async def test_several_things_from_one_message_without_a_single_yes(h, monkeypatch):
    use_router(monkeypatch, {"do": [
        {"action": "assign", "args": {"who": "E-0003", "text": "backend'ни"}},
        {"action": "assign", "args": {"who": "E-0004", "text": "ҳисоботни"}},
    ], "say": None})
    await h.say(OWNER, "Анварга backend'ни, Каримга ҳисоботни бер")
    assert "«backend'ни»" in h.to(MENTOR)[-1] and "«ҳисоботни»" in h.to(BOSS)[-1]
    assert not any("Тасдиқлайсизми" in t for t in h.to(OWNER))
    async with db.session_scope() as s:
        assert len((await s.execute(select(Task))).scalars().all()) == 2
    no_buttons(h)


async def test_it_looks_up_the_data_and_answers_with_names(h, monkeypatch):
    use_router(monkeypatch, {"action": "assign", "args": {"who": "E-0003", "text": "backend'ни қилсин"}})
    await h.say(OWNER, "Анвар backend'ни қилсин")
    async with db.session_scope() as s:
        task = (await s.execute(select(Task))).scalar_one()
        s.add(TaskUpdate(task_id=task.id, text="ярмини қилдим"))

    router = use_router(monkeypatch,
                        {"look": [{"tool": "activity", "args": {"who": "E-0003"}}]},
                        {"say": "E-0003 бугун «backend'ни қилсин» бўйича ёзди: ярмини қилган."})
    replies = await h.say(OWNER, "Анвар бугун нима қилди?")
    assert replies == ["Устозов Анвар бугун «backend'ни қилсин» бўйича ёзди: ярмини қилган."]
    looked = router.prompts[1].split("LOOKED", 1)[1]
    data = router.prompts[1].split("CONTEXT (set by the server)", 1)[1]  # what the server sent, not the rules
    assert "ярмини қилдим" in looked and "E-0003" in looked and "Анвар" not in data


async def test_a_tool_this_person_may_not_use_returns_nothing(h, monkeypatch):
    router = use_router(monkeypatch, {"look": [{"tool": "staff", "args": {}}]}, {"say": "Билмайман."})
    await h.say(MENTOR, "ҳамма ходимлар ким?")
    assert '"not available to this person"' in router.prompts[1] and "Дастурчи" not in router.prompts[1]


async def test_risky_steps_wait_for_one_yes_together(h, monkeypatch):
    use_router(monkeypatch, {"do": [
        {"action": "role_change", "args": {"who": "E-0003", "role": "manager"}},
        {"action": "role_change", "args": {"who": "E-0004", "role": "hr"}},
    ]})
    replies = await h.say(OWNER, "Анварни раҳбар, Каримни HR қил")
    assert len(replies) == 1 and "1. " in replies[0] and "2. " in replies[0] and "Тасдиқланг." in replies[0]
    assert "Устозов Анвар" in replies[0] and "Бошлиқов Карим" in replies[0]
    assert (await _role(MENTOR), await _role(BOSS)) == ("employee", "manager")  # nothing before «ҳа»
    await h.say(OWNER, "ҳа")
    assert (await _role(MENTOR), await _role(BOSS)) == ("manager", "hr")


async def test_no_to_the_risky_steps_changes_nothing(h, monkeypatch):
    use_router(monkeypatch, {"do": [
        {"action": "role_change", "args": {"who": "E-0003", "role": "manager"}},
        {"action": "role_change", "args": {"who": "E-0004", "role": "hr"}},
    ]})
    await h.say(OWNER, "Анварни раҳбар, Каримни HR қил")
    await h.say(OWNER, "йўқ")
    assert (await _role(MENTOR), await _role(BOSS)) == ("employee", "manager")


async def test_it_remembers_the_conversation(h, monkeypatch):
    use_router(monkeypatch, {"action": "assign", "args": {"who": "E-0003", "text": "backend'ни қилсин"}})
    await h.say(OWNER, "Анвар backend'ни қилсин")
    router = use_router(monkeypatch, {"action": "assign", "args": {"who": "E-0004", "text": None}})
    await h.say(OWNER, "Каримга ҳам")
    conversation = router.prompts[0].split("RECENT CONVERSATION (oldest first)", 1)[1]
    assert "backend'ни қилсин" in conversation and "E-0003" in conversation and "Анвар" not in conversation


async def test_exact_commands_need_no_ai(h, monkeypatch):
    router = use_router(monkeypatch, {"action": "staff_list", "args": {}})
    await h.say(OWNER, "/topshiriqlar")
    await h.say(OWNER, "топшириқлар")
    assert router.prompts == []


async def test_what_the_ai_says_is_checked_before_it_is_sent(h, monkeypatch):
    use_router(monkeypatch, {"say": "Картангиз: 8600 1234 5678 9012"})
    replies = await h.say(OWNER, "нима гап?")
    assert not any("8600" in r for r in replies)


# --------------------------------------------------------------------------- «Тасдиқланг»

async def _ask_two_roles(h, monkeypatch):
    use_router(monkeypatch, {"do": [
        {"action": "role_change", "args": {"who": "E-0003", "role": "manager"}},
        {"action": "role_change", "args": {"who": "E-0004", "role": "hr"}},
    ]})
    replies = await h.say(OWNER, "Анварни раҳбар, Каримни HR қил")
    assert replies[0].rstrip().endswith("Тасдиқланг.") and "«ҳа»" not in replies[0]


async def test_a_reply_in_own_words_is_read_by_the_ai(h, monkeypatch):
    await _ask_two_roles(h, monkeypatch)
    use_router(monkeypatch, {"means": "yes"})
    await h.say(OWNER, "бўпти, қайта сўрама, қилавер")
    assert (await _role(MENTOR), await _role(BOSS)) == ("manager", "hr")


async def test_something_else_drops_the_waiting_step_quietly(h, monkeypatch):
    await _ask_two_roles(h, monkeypatch)
    use_router(monkeypatch, {"means": "other"}, {"action": "tasks", "args": {}})
    replies = await h.say(OWNER, "менга берилган ишларни кўрсат")
    assert not any("бажармадим" in r or "«ҳа»" in r for r in replies)
    assert (await _role(MENTOR), await _role(BOSS)) == ("employee", "manager")


async def test_a_yes_the_next_day_does_not_run_an_old_step(h, monkeypatch):
    from datetime import timedelta

    from hari.worktime import now_utc

    await _ask_two_roles(h, monkeypatch)
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=h.bot.id, chat_id=OWNER, user_id=OWNER)
    data = await h.dp.storage.get_data(key)
    data["pending"]["at"] = (now_utc() - timedelta(hours=20)).isoformat()
    await h.dp.storage.set_data(key, data)
    use_router(monkeypatch)
    await h.say(OWNER, "ҳа")
    assert (await _role(MENTOR), await _role(BOSS)) == ("employee", "manager")


async def test_the_morning_message_has_no_tail(h):
    from hari import agenda

    assert "ёрдам бераман" not in __import__("inspect").getsource(agenda.morning_brief)


# --------------------------------------------------------------------------- a clear order is done; the morning list is remembered

@pytest.mark.parametrize("text,action,done", [
    ("ochir unda", "app_purge", True),
    ("Валининг маълумотини ўчир", "app_purge", True),
    ("hammasini rad et", "close_all", True),
    ("булар бажарилган", "close_all", True),
    ("Валини рад эт", "app_reject", True),
    ("Вали ҳақида нима дейсиз?", "app_reject", False),
    ("Анварни раҳбар қил", "role_change", False),  # a role always waits for «Тасдиқланг»
])
def test_a_clear_order_needs_no_confirmation(text, action, done):
    from hari.bot.handlers.chat import ordered

    assert ordered(text, action) is done


async def test_the_morning_list_is_remembered_for_what_comes_next(h, session_scope):
    from hari import memory, scheduler
    from hari.worktime import now_utc

    async with session_scope() as s:
        owner = (await s.execute(select(Employee).where(Employee.telegram_user_id == OWNER))).scalar_one()
        await scheduler.morning_briefs(s, now_utc(), [(owner.id, "brief:test", "Хайрли тонг. Мурожаатлар: …")])
    async with session_scope() as s:
        turns = await memory.recent(s, owner, now_utc())
    assert turns[-1][1] == "Хайрли тонг. Мурожаатлар: …"
