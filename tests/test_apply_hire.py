"""«Ишга олиш»: the applicant becomes an employee card, gets the one-time link, and onboarding starts."""

from __future__ import annotations

import re

from sqlalchemy import func, select

from hari.db.models import Application, Employee
from tests.apply_fixtures import APPLICANT, HR, OWNER, h_apply, h_apply_done  # noqa: F401
from tests.test_bot_flow import btn


async def fill_card(h, tg: int) -> list[str]:  # noqa: ANN001
    replies = await h.press(tg, btn(h, tg, "Ишга олиш"))
    assert "Номзоддан карточка: Алиев Вали, B2B савдо менежери" in replies[0] and "Бўлим" in replies[0]
    replies = await h.say(tg, "Савдо")  # the starting vacancies have no department; the position is known
    assert "4/8" in replies[0]
    await h.press(tg, btn(h, tg, "Ходим"))
    await h.press(tg, btn(h, tg, "— Йўқ"))
    await h.press(tg, btn(h, tg, "— Йўқ"))
    await h.say(tg, "бугун")
    await h.press(tg, btn(h, tg, "Ўзбек (кирилл)"))
    return await h.press(tg, btn(h, tg, "Сақлаш"))


async def test_hire_turns_applicant_into_employee(h_apply_done, session_scope):
    h = h_apply_done
    replies = await fill_card(h, OWNER)
    assert "Ҳавола номзодга юборилди" in replies[-1]
    link = re.search(r"https://t\.me/mgmHaribot\?start=(\S+)", h.to(APPLICANT)[-1])
    assert link and "ишга қабул қилиндингиз" in h.to(APPLICANT)[-1]
    replies = await h.say(APPLICANT, f"/start {link.group(1)}")
    assert "Хуш келибсиз, Алиев Вали" in replies[0] and "B2B савдо менежери" in replies[0]
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
        emp = await s.get(Employee, app.employee_id)
    assert app.status == "hired" and emp.full_name == "Алиев Вали" and emp.department == "Савдо"
    await h.press(HR, btn(h, HR, "Рад этиш"))
    assert any("аллақачон" in a and "Ишга олинган" in a for a in h.session.alerts)


async def test_hire_after_reject_creates_nothing(h_apply_done, session_scope):
    h = h_apply_done
    async with session_scope() as s:
        before = (await s.execute(select(func.count()).select_from(Employee))).scalar_one()
    hire = btn(h, OWNER, "Ишга олиш")
    await h.press(HR, btn(h, HR, "Рад этиш"))
    replies = await h.press(OWNER, hire)
    assert any("аллақачон" in a for a in h.session.alerts) and replies == []
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Employee))).scalar_one() == before


async def test_decision_while_the_card_is_open_wins(h_apply_done, session_scope):
    h = h_apply_done
    await h.press(OWNER, btn(h, OWNER, "Ишга олиш"))
    await h.say(OWNER, "Савдо")
    await h.press(OWNER, btn(h, OWNER, "Ходим"))
    await h.press(OWNER, btn(h, OWNER, "— Йўқ"))
    await h.press(OWNER, btn(h, OWNER, "— Йўқ"))
    await h.say(OWNER, "бугун")
    await h.press(OWNER, btn(h, OWNER, "Ўзбек (кирилл)"))
    async with session_scope() as s:
        before = (await s.execute(select(func.count()).select_from(Employee))).scalar_one()
    await h.press(HR, btn(h, HR, "Рад этиш"))  # HR rejects while the owner is still on the card
    replies = await h.press(OWNER, btn(h, OWNER, "Сақлаш"))
    assert "аллақачон" in replies[-1]
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Employee))).scalar_one() == before
    assert not any("ишга қабул қилиндингиз" in m for m in h.to(APPLICANT))
