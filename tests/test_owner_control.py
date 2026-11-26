"""The owner hears about every newcomer and runs each card by writing: the report when someone joins,
adding someone who opened HARI without a card, and changing position, department and role."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from hari.db import session as db
from hari.db.models import Employee
from hari.employees import by_telegram, make_invite
from tests.test_bot_flow import HR, MENTOR, STRANGER, Harness, no_buttons  # noqa: F401

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
    for part in ("Янги ходим HARI'га қўшилди", "Янгиев Ботир", "Telegram: уланган",
                 "Лавозим: Сотувчи", "Бўлим: Garmin сотув", "Роль: Ходим"):
        assert part in report, part
    assert "лавозими: …" not in report and "қил»" not in report  # no instructions under the card
    assert emp.code not in report and "777" not in report  # no codes or Telegram numbers
    assert not any("қўшилди" in t for t in h.to(HR))  # the report goes to owners

    replies = await h.say(OWNER, "Ботирнинг лавозими: Катта сотувчи")
    assert "Тасдиқланг." in replies[0] and "«Сотувчи» → «Катта сотувчи»" in replies[0]
    replies = await h.say(OWNER, "ҳа")
    assert "«Сотувчи» → «Катта сотувчи»" in replies[0] and "Лавозим: Катта сотувчи" in replies[0]
    assert "лавозим — Катта сотувчи" in h.to(777)[-1]  # the person is told
    async with session_scope() as s:
        assert (await by_telegram(s, 777)).position == "Катта сотувчи"
    no_buttons(h)


async def test_someone_without_a_card_is_reported_once_and_the_owner_adds_them(h, session_scope):
    await h.say(STRANGER, "/start")
    report = h.to(OWNER)[-1]
    assert "профили йўқ одам" in report and "u504" in report and "қилиб қўш»" not in report
    assert "Telegram ID" not in report  # added by name, no Telegram number shown
    await h.say(STRANGER, "/start")
    assert sum("профили йўқ" in t for t in h.to(OWNER)) == 1  # once per account, not on every /start

    await h.say(OWNER, "ходим қилиб қўш")  # the one person waiting: found without a number
    await h.say(OWNER, "Омборчи")  # 1/4 position
    await h.say(OWNER, "Омборов Жасур")  # 2/4 name
    await h.say(OWNER, "Омбор")  # 3/4 department, typed
    await h.say(OWNER, "Ходим")  # 4/4 role
    replies = await h.say(OWNER, "ҳа")
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
    await h.say(OWNER, f"{STRANGER} ни ходим қилиб қўш")
    await h.say(OWNER, "Сотувчи")
    replies = await h.say(OWNER, "Сотувов Али")
    garmin = next(line.split(".")[0] for line in replies[0].splitlines() if line.endswith("Garmin сотув"))
    await h.say(OWNER, garmin)  # the department by its number in the list
    await h.say(OWNER, "1")  # the role by its number
    await h.say(OWNER, "ҳа")
    async with session_scope() as s:
        assert (await by_telegram(s, STRANGER)).department == "Garmin сотув"


async def test_hr_cannot_make_an_owner_or_change_roles(h, session_scope):
    await h.say(STRANGER, "/start")
    await h.say(HR, f"{STRANGER} ни ходим қилиб қўш")
    await h.say(HR, "Директор")
    await h.say(HR, "Ёлғон Эга")
    replies = await h.say(HR, "Раҳбарият")
    assert "Эгаси" not in replies[0]  # an owner role is not offered to HR
    assert "Тушунмадим" in (await h.say(HR, "эгаси"))[0]  # and typing it does not work either
    await h.say(HR, "бекор")
    async with session_scope() as s:
        assert await by_telegram(s, STRANGER) is None
    assert (await h.say(HR, "E-0003 ролини owner қил"))[0] == "Бу амал учун ҳуқуқингиз йўқ."
    assert (await h.say(HR, "/rol E-0003 owner"))[0] == "Бу амал учун ҳуқуқингиз йўқ."
    async with db.session_scope() as s:
        owners = (await s.execute(select(Employee).where(Employee.role == "owner"))).scalars().all()
        assert [o.telegram_user_id for o in owners] == [OWNER]


async def test_owner_changes_a_role_by_writing_with_the_one_time_confirmation(h, session_scope):
    replies = await h.say(OWNER, f"/profil {MENTOR}")
    assert "Устозов Анвар" in replies[0] and "Роль: Ходим" in replies[0]
    replies = await h.say(OWNER, "E-0003 ролини раҳбар қил")
    assert "Роль ўзгариши лойиҳаси" in replies[0] and "Ходим → Раҳбар" in replies[0]
    async with session_scope() as s:
        assert (await by_telegram(s, MENTOR)).role == "employee"  # a draft until «ҳа»
    assert "роль — Раҳбар" in (await h.say(OWNER, "ҳа"))[0]
    async with session_scope() as s:
        assert (await by_telegram(s, MENTOR)).role == "manager"
    # the same «ҳа» again changes nothing: no draft is waiting any more
    assert not any("роль —" in r for r in await h.say(OWNER, "ҳа"))


async def test_hr_edits_department_and_employees_see_nothing(h):
    replies = await h.say(HR, "E-0003 профили")
    assert "Устозов Анвар" in replies[0] and "ролини" not in replies[0]  # only an owner changes roles
    replies = await h.say(HR, "E-0003 бўлими")
    assert "ҳозирги бўлим — «Garmin сотув»" in replies[0] and "Янгисини ёзинг" in replies[0]
    assert "«Londry»" in (await h.say(HR, "Londry"))[0]  # what will change, then «ҳа»
    assert "Бўлим: Londry" in (await h.say(HR, "ҳа"))[0]
    assert (await h.say(MENTOR, "/profil E-0001"))[0] == "Бу амал учун ҳуқуқингиз йўқ."


async def test_staff_list_is_just_the_list(h):
    replies = await h.say(OWNER, "ходимлар")
    assert "профили»" not in replies[0] and "E-000" not in replies[0]


# --------------------------------------------------------------------------- no Menu button

def _menus(h: Harness) -> dict[int, list[str]]:
    """Wrap the fake Telegram so the command lists HARI sets or removes are recorded per chat."""
    from aiogram.methods import DeleteMyCommands, SetMyCommands

    menus: dict[int, list[str]] = {}
    real = h.session.make_request

    async def record(bot, method, timeout=None):  # noqa: ANN001, ANN202
        if isinstance(method, SetMyCommands):
            menus[getattr(method.scope, "chat_id", 0)] = [c.command for c in method.commands]
        if isinstance(method, DeleteMyCommands):
            menus[getattr(method.scope, "chat_id", 0)] = []
        return await real(bot, method, timeout)

    h.session.make_request = record
    return menus


async def test_a_small_menu_for_each_kind_of_person(h):
    from hari.bot import commands

    menus = _menus(h)
    await commands.install(h.bot)
    assert menus[0] == []  # no card, no menu
    assert menus[OWNER] == menus[HR] == ["topshiriqlar", "arizalar", "navbat", "xodimlar"]
    assert menus[MENTOR] == ["topshiriqlar", "reja", "murojaat", "murojaatlarim"]  # «профилим» needs no slash
    assert all(len(m) <= 4 for m in menus.values())
    from hari import intent as I

    for command in {c for m in menus.values() for c in m}:  # every one of them is a real command
        assert I.direct(f"/{command}") is not None, command


async def test_the_menu_follows_a_new_role(h):
    from hari.bot import commands

    menus = _menus(h)
    await commands.install(h.bot)
    await h.say(OWNER, "Анварни HR қил")
    await h.say(OWNER, "ҳа")
    await h.say(MENTOR, "салом")  # their next message brings the new menu
    assert menus[MENTOR] == ["topshiriqlar", "arizalar", "navbat", "xodimlar"]


async def test_slash_commands_still_work_when_typed(h):
    assert "мослашув режаси бириктирилмаган" in (await h.say(MENTOR, "/reja"))[0]
    assert "Устозов Анвар" in (await h.say(MENTOR, "/profilim"))[0]
    assert "Қандай мурожаат?" in (await h.say(MENTOR, "/murojaat"))[0]
    assert (await h.say(HR, "/navbat"))[0] == "Иш навбатингиз бўш."
