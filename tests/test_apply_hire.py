"""«Ишга олиш»: the applicant becomes an employee card, gets the one-time link, and onboarding starts."""

from __future__ import annotations

import re

from sqlalchemy import func, select

from hari.db.models import Application, Employee
from tests.apply_fixtures import APPLICANT, HR, OWNER, h_apply, h_apply_done, reject  # noqa: F401


async def app_number(session_scope) -> str:
    async with session_scope() as s:
        return (await s.execute(select(Application))).scalar_one().number


async def start_card(h, tg: int, number: str) -> list[str]:  # noqa: ANN001
    replies = await h.say(tg, f"{number} ни ишга ол")
    assert "Номзоддан карточка: Алиев Вали, B2B савдо менежери" in replies[0] and "Бўлим" in replies[0]
    replies = await h.say(tg, "Савдо")  # the starting vacancies have no department; the position is known
    assert "4/8" in replies[0]
    await h.say(tg, "ходим")
    await h.say(tg, "йўқ")
    await h.say(tg, "йўқ")
    await h.say(tg, "бугун")
    return await h.say(tg, "1")


async def fill_card(h, tg: int, number: str) -> list[str]:  # noqa: ANN001
    await start_card(h, tg, number)
    return await h.say(tg, "ҳа")


async def test_hire_turns_applicant_into_employee(h_apply_done, session_scope):
    h = h_apply_done
    number = await app_number(session_scope)
    replies = await fill_card(h, OWNER, number)
    assert "Ҳавола номзодга юборилди" in replies[-1]
    link = re.search(r"https://t\.me/mgmHaribot\?start=(\S+)", h.to(APPLICANT)[-1])
    assert link and "ишга қабул қилиндингиз" in h.to(APPLICANT)[-1]
    replies = await h.say(APPLICANT, f"/start {link.group(1)}")
    assert "Хуш келибсиз, Алиев Вали" in replies[0] and "B2B савдо менежери" in replies[0]
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
        emp = await s.get(Employee, app.employee_id)
    assert app.status == "hired" and emp.full_name == "Алиев Вали" and emp.department == "Савдо"
    replies = await h.say(HR, f"{number} рад эт: кеч")
    assert "аллақачон" in replies[0] and "Ишга олинган" in replies[0]


async def test_hire_after_reject_creates_nothing(h_apply_done, session_scope):
    h = h_apply_done
    number = await app_number(session_scope)
    async with session_scope() as s:
        before = (await s.execute(select(func.count()).select_from(Employee))).scalar_one()
    await reject(h, HR, number=number)
    replies = await h.say(OWNER, f"{number} ни ишга ол")
    assert "аллақачон" in replies[0] and "Номзоддан карточка" not in replies[0]
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Employee))).scalar_one() == before


async def test_decision_while_the_card_is_open_wins(h_apply_done, session_scope):
    h = h_apply_done
    number = await app_number(session_scope)
    await start_card(h, OWNER, number)
    async with session_scope() as s:
        before = (await s.execute(select(func.count()).select_from(Employee))).scalar_one()
    await reject(h, HR, number=number)  # HR rejects while the owner is still on the card
    replies = await h.say(OWNER, "ҳа")
    assert "аллақачон" in replies[-1]
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Employee))).scalar_one() == before
    assert not any("ишга қабул қилиндингиз" in m for m in h.to(APPLICANT))
