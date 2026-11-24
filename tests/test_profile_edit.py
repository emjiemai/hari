"""A card is changed by saying so — several things at once, one's own or (HR, owner) anyone's — after «ҳа»,
and every change is kept: what it was, what it became, who changed it and when. No instructions under cards."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from hari import profile as P
from hari.db.models import Employee, ProfileChange
from tests.test_bot_flow import Harness

OWNER, HR, EMP = 841, 842, 843


@pytest.fixture
async def h(session_scope, staff):
    async with session_scope() as s:
        await staff(s, "Familiya Ism", "owner", department="Rahbariyat", position="Admin", tg=OWNER)
        await staff(s, "Кадрова Зайнаб", "hr", department="HR", position="HR масъули", tg=HR)
        await staff(s, "Аълоев Анвар", position="Сотувчи", tg=EMP)
    return Harness()


async def card(session_scope, tg: int) -> Employee:
    async with session_scope() as s:
        return (await s.execute(select(Employee).where(Employee.telegram_user_id == tg))).scalar_one()


async def changes(session_scope, emp_id: str) -> list[ProfileChange]:
    async with session_scope() as s:
        return list((await s.execute(select(ProfileChange).where(ProfileChange.employee_id == emp_id)
                                     .order_by(ProfileChange.at))).scalars())


@pytest.mark.parametrize("text, expected", [
    ("Ismimni Eshonov Shahzod qil, lavozimim Direktor", [("name", "Eshonov Shahzod"), ("position", "Direktor")]),
    ("Анварнинг лавозими Сотувчи, бўлими Garmin бўлсин", [("position", "Сотувчи"), ("department", "Garmin")]),
    ("поменяй мою должность на Директор", [("position", "Директор")]),
    ("лавозимим Бўлим бошлиғи бўлсин", [("position", "Бўлим бошлиғи")]),  # a field's name inside a value
    ("лавозимимни Директорга ўзгартир", [("position", "Директор")]),
    ("иш кунларим Ду-Жу, иш вақтим 8-17", [("days", "Ду-Жу"), ("hours", "8-17")]),
    ("ishga kelgan sanam 10.11.2026", [("start_date", "10.11.2026")]),
    ("лавозимим нима?", [("position", None)]),  # a question changes nothing
    ("Анварни раҳбар қил", []),  # a role, not the manager field
])
def test_what_a_message_asks_to_change(text, expected):
    assert [(c["field"], c["value"]) for c in P.parse(text).changes] == expected


def test_values_are_understood():
    assert P._days("Ду-Жу") == "0,1,2,3,4" and P._days("душанбадан шанбагача") == "0,1,2,3,4,5"
    assert P._hours("9 дан 18 гача") == (9, 18) and P._date("10 ноябрь 2026") == date(2026, 11, 10)


async def test_the_owner_changes_several_things_on_their_own_card_at_once(h, session_scope):
    replies = await h.say(OWNER, "Исмимни Эшонов Шахзод Аббосович қил, лавозимим Директор, бўлимим Раҳбарият")
    reply = replies[0]
    assert "Тасдиқлайсизми?" in reply and "«Familiya Ism» → «Эшонов Шахзод Аббосович»" in reply
    assert "«Admin» → «Директор»" in reply and "«Rahbariyat» → «Раҳбарият»" in reply
    assert (await card(session_scope, OWNER)).position == "Admin"  # nothing yet
    replies = await h.say(OWNER, "ҳа")
    assert "Сақланди" in replies[0]
    me = await card(session_scope, OWNER)
    assert (me.full_name, me.position, me.department) == ("Эшонов Шахзод Аббосович", "Директор", "Раҳбарият")
    kept = await changes(session_scope, me.id)
    assert [(c.field, c.old_value, c.new_value, c.changed_by) for c in kept] == [
        ("name", "Familiya Ism", "Эшонов Шахзод Аббосович", me.id), ("position", "Admin", "Директор", me.id),
        ("department", "Rahbariyat", "Раҳбарият", me.id)]


async def test_hr_changes_someone_elses_manager_hours_and_days(h, session_scope):
    replies = await h.say(HR, "Анварнинг раҳбари Кадрова Зайнаб бўлсин, иш вақти 8-17, иш кунлари Ду-Жу")
    assert "Тасдиқлайсизми?" in replies[0] and "«—» → «Кадрова Зайнаб»" in replies[0]
    await h.say(HR, "ҳа")
    anvar, hr = await card(session_scope, EMP), await card(session_scope, HR)
    assert (anvar.manager_id, anvar.work_start_hour, anvar.work_end_hour, anvar.work_days) == (hr.id, 8, 17, "0,1,2,3,4")
    assert "раҳбар — Кадрова Зайнаб" in h.to(EMP)[-1]  # the person is told
    kept = await changes(session_scope, anvar.id)
    assert {(c.field, c.new_value) for c in kept} == {("manager", "Кадрова Зайнаб"), ("hours", "8:00–17:00"),
                                                     ("days", "Ду, Се, Чо, Па, Жу")}
    assert {c.changed_by for c in kept} == {hr.id}


async def test_no_answers_no_change(h, session_scope):
    await h.say(OWNER, "лавозимим: Директор")
    await h.say(OWNER, "йўқ")
    assert (await card(session_scope, OWNER)).position == "Admin"
    assert await changes(session_scope, (await card(session_scope, OWNER)).id) == []


async def test_ones_own_profile_has_no_instructions_under_it(h):
    for tg in (OWNER, HR, EMP):
        reply = (await h.say(tg, "профилим"))[-1]
        assert "деб ёзинг" not in reply and "Ўзгартириш" not in reply and "«" not in reply, reply


async def test_an_employee_asks_hr_instead_of_changing_it(h, session_scope):
    await h.say(EMP, "лавозимимни Катта сотувчи қил")  # HR gets the request at once
    assert "Профилимдаги хатони тузатиш" in h.to(HR)[-1] and "Катта сотувчи" in h.to(HR)[-1]
    assert (await card(session_scope, EMP)).position == "Сотувчи"


async def test_language_and_role_changes_are_kept_and_the_history_can_be_read(h, session_scope):
    await h.say(EMP, "лотинда ёзинг")
    anvar = await card(session_scope, EMP)
    assert [(c.field, c.new_value, c.how) for c in await changes(session_scope, anvar.id)] == [
        ("language", "ўзбек (лотин)", "language")]
    await h.say(OWNER, "Анварни раҳбар қил")
    await h.say(OWNER, "ҳа")
    assert ("role", "Ходим", "Раҳбар") in {(c.field, c.old_value, c.new_value)
                                           for c in await changes(session_scope, anvar.id)}
    history = (await h.say(OWNER, "Анварнинг профил ўзгаришлари тарихи"))[-1]
    assert "Аълоев Анвар" in history and "Ходим" in history and "Раҳбар" in history
    mine = (await h.say(EMP, "profilim tarixi"))[-1]
    assert "lotin" in mine.lower()


def test_the_routers_changes_are_checked_against_the_message():
    from hari import intent as I

    out = I._changes([{"field": "position", "value": "Директор"}, {"field": "manager", "value": "E-0001"},
                      {"field": "name", "value": "Ўйлаб топилган Исм"}, {"field": "role", "value": "owner"}],
                     "лавозимим Директор, раҳбарим Эгаев", {"E-0001"})
    assert out == [{"field": "position", "value": "Директор"}, {"field": "manager", "value": "E-0001"},
                   {"field": "name", "value": None}]  # an invented value is dropped, a role is never changed here
