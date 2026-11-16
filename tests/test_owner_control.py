"""The owner hears about every newcomer and runs each card from the bot: the report when someone joins,
adding someone who opened HARI without a card, and changing position, department and role."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from hari.bot.ui import JoinCb, PickCb, RoleCb
from hari.db import session as db
from hari.db.models import Employee
from hari.employees import by_telegram, make_invite
from tests.test_bot_flow import HR, MENTOR, STRANGER, Harness, btn  # noqa: F401

OWNER = 600


@pytest.fixture
async def h(session_scope, staff):
    from hari.bot import common

    common.bot_username = "mgmHaribot"
    async with session_scope() as s:
        await staff(s, "Эгаси Шахзод", "owner", department="Раҳбарият", position="Директор", tg=OWNER)
        await staff(s, "Кадрова Зайнаб", "hr", department="HR", position="HR масъули", tg=HR)
        await staff(s, "Устозов Анвар", position="Катта сотувчи", tg=MENTOR)
    return Harness()


async def test_owner_gets_the_full_card_when_someone_joins_and_sets_the_position(h, session_scope, staff):
    async with session_scope() as s:
        emp = await staff(s, "Янгиев Ботир", position="Сотувчи")
        token = await make_invite(s, emp, created_by=None)
    await h.say(777, f"/start {token}")
    report = h.to(OWNER)[-1]
    for part in ("Янги ходим HARI'га қўшилди", "Янгиев Ботир", f"ID: {emp.code}", "Telegram ID: 777",
                 "Лавозим: Сотувчи", "Бўлим: Garmin сотув", "Роль: Ходим"):
        assert part in report, part
    assert [t for t, _ in h.buttons(OWNER)] == ["Лавозимни белгилаш", "Бўлимни ўзгартириш", "Ролни ўзгартириш"]
    assert not any("қўшилди" in t for t in h.to(HR))  # the report goes to owners

    await h.press(OWNER, btn(h, OWNER, "Лавозимни белгилаш"))
    replies = await h.say(OWNER, "Катта сотувчи")
    assert "«Сотувчи» → «Катта сотувчи»" in replies[0] and "Лавозим: Катта сотувчи" in replies[0]
    assert "лавозим — Катта сотувчи" in h.to(777)[-1]  # the person is told
    async with session_scope() as s:
        assert (await by_telegram(s, 777)).position == "Катта сотувчи"


async def test_someone_without_a_card_is_reported_once_and_the_owner_adds_them(h, session_scope):
    await h.say(STRANGER, "/start")
    report = h.to(OWNER)[-1]
    assert "профили йўқ одам" in report and f"Telegram ID: {STRANGER}" in report and "u504" in report
    await h.say(STRANGER, "/start")
    assert sum("профили йўқ" in t for t in h.to(OWNER)) == 1  # once per account, not on every /start

    await h.press(OWNER, btn(h, OWNER, "Ходим қилиб қўшиш"))
    await h.say(OWNER, "Омборчи")  # 1/4 position
    await h.say(OWNER, "Омборов Жасур")  # 2/4 name
    await h.say(OWNER, "Омбор")  # 3/4 department, typed
    await h.press(OWNER, btn(h, OWNER, "Ходим"))  # 4/4 role
    replies = await h.press(OWNER, btn(h, OWNER, "Сақлаш"))
    assert "Ходим қўшилди" in replies[0] and "Лавозим: Омборчи" in replies[0]
    assert "Хуш келибсиз, Омборов Жасур" in h.to(STRANGER)[-1]
    async with session_scope() as s:
        emp = await by_telegram(s, STRANGER)
        assert (emp.position, emp.department, emp.role, emp.status) == ("Омборчи", "Омбор", "employee", "active")
    assert (await h.say(STRANGER, "/xodimlar"))[0] == "Бу амал учун ҳуқуқингиз йўқ."  # an employee, not HR
    replies = await h.say(STRANGER, "Режам")
    assert "Мослашув режаси" in replies[0]


async def test_a_department_can_be_picked_from_the_existing_ones(h, session_scope):
    await h.say(STRANGER, "/start")
    await h.press(OWNER, btn(h, OWNER, "Ходим қилиб қўшиш"))
    await h.say(OWNER, "Сотувчи")
    await h.say(OWNER, "Сотувов Али")
    await h.press(OWNER, btn(h, OWNER, "Garmin сотув"))
    await h.press(OWNER, btn(h, OWNER, "Ходим"))
    await h.press(OWNER, btn(h, OWNER, "Сақлаш"))
    async with session_scope() as s:
        assert (await by_telegram(s, STRANGER)).department == "Garmin сотув"


async def test_hr_cannot_make_an_owner_or_change_roles_with_forged_buttons(h, session_scope):
    await h.say(STRANGER, "/start")
    await h.press(HR, JoinCb(action="add", tg=STRANGER).pack())
    await h.say(HR, "Директор")
    await h.say(HR, "Ёлғон Эга")
    await h.say(HR, "Раҳбарият")
    assert "Эгаси" not in [t for t, _ in h.buttons(HR)]
    await h.press(HR, PickCb(field="addrole", value="owner").pack())  # forged
    replies = await h.press(HR, PickCb(field="addok", value="yes").pack())
    assert "ҳуқуқ" in replies[-1].lower()
    async with session_scope() as s:
        assert await by_telegram(s, STRANGER) is None
        mentor = await by_telegram(s, MENTOR)
    await h.press(HR, RoleCb(action="set", emp_id=mentor.id, role="owner").pack())
    assert "ҳуқуқ" in h.session.alerts[-1].lower()
    async with db.session_scope() as s:
        assert (await s.execute(select(Employee).where(Employee.role == "owner"))).scalars().all()[0].telegram_user_id == OWNER


async def test_owner_changes_a_role_from_the_card_with_the_one_time_confirmation(h, session_scope):
    replies = await h.say(OWNER, f"/profil {MENTOR}")
    assert "Устозов Анвар" in replies[0] and "Роль: Ходим" in replies[0]
    await h.press(OWNER, btn(h, OWNER, "Ролни ўзгартириш"))
    await h.press(OWNER, btn(h, OWNER, "Раҳбар"))
    assert "Роль ўзгариши лойиҳаси" in h.to(OWNER)[-1]
    await h.press(OWNER, btn(h, OWNER, "Тасдиқлаш"))
    assert "роль — Раҳбар" in h.to(OWNER)[-1]
    async with session_scope() as s:
        assert (await by_telegram(s, MENTOR)).role == "manager"


async def test_hr_edits_position_but_sees_no_role_button_and_employees_see_nothing(h):
    replies = await h.say(HR, "/profil E-0003")
    assert "Устозов Анвар" in replies[0]
    assert [t for t, _ in h.buttons(HR)] == ["Лавозимни белгилаш", "Бўлимни ўзгартириш"]
    await h.press(HR, btn(h, HR, "Бўлимни ўзгартириш"))
    assert "Бўлим: Londry" in (await h.say(HR, "Londry"))[0]
    assert (await h.say(MENTOR, "/profil E-0001"))[0] == "Бу амал учун ҳуқуқингиз йўқ."


async def test_staff_list_points_to_the_profile_command(h):
    replies = await h.say(OWNER, "/xodimlar")
    assert re.search(r"/profil E-\d{4}", replies[0])
