"""«Нечта одам базада бор?» is answered from HARI's own database (hari.facts): counts only, no names, only what
the person may see, and the database is named as the source when asked."""

from __future__ import annotations

import json

from hari import cases as C
from hari.answer import answer_question
from hari.db.models import Employee
from hari.facts import facts_for
from hari.worktime import now_utc
from tests.test_answer import FakeLLM


async def team(session_scope, staff):  # noqa: ANN001
    async with session_scope() as s:
        owner = await staff(s, "Эгаев Улуғ", "owner", department="Раҳбарият", tg=11)
        hr = await staff(s, "Кадрова Зайнаб", "hr", department="HR", tg=12)
        emp = await staff(s, "Аълоев Анвар", tg=13)
        await staff(s, "Янгиев Ботир")  # not linked to Telegram yet
        await C.open_case(s, emp, "complaint", "директор ҳақида", about_role="owner")
        await C.open_case(s, emp, "certificate", "маълумотнома")
        return owner.id, hr.id, emp.id


async def test_hr_gets_company_counts_without_names(session_scope, staff):
    _, hr_id, emp_id = await team(session_scope, staff)
    async with session_scope() as s:
        facts = await facts_for(s, await s.get(Employee, hr_id), now_utc())
        mine = await facts_for(s, await s.get(Employee, emp_id), now_utc())
    people = facts["employees"]
    assert people["in_database"] == 4 and people["linked_to_telegram"] == 3 and people["not_linked_yet"] == 1
    assert people["by_role"] == {"owner": 1, "hr": 1, "employee": 2}
    blob = json.dumps(facts, ensure_ascii=False)
    assert all(name not in blob for name in ("Эгаев", "Кадрова", "Аълоев", "Янгиев", "Анвар"))
    assert "employees" not in mine and mine["my_open_hr_requests"] == 2  # an employee: only their own


async def test_a_complaint_about_the_owner_is_not_even_a_number_to_the_owner(session_scope, staff):
    owner_id, hr_id, _ = await team(session_scope, staff)
    async with session_scope() as s:
        owner = await facts_for(s, await s.get(Employee, owner_id), now_utc())
        hr = await facts_for(s, await s.get(Employee, hr_id), now_utc())
    assert owner["hr_requests"]["open"] == 1 and hr["hr_requests"]["open"] == 2


async def test_how_many_people_is_answered_from_the_database(session_scope, staff, today):
    _, hr_id, _ = await team(session_scope, staff)
    llm = FakeLLM({"answer": "Базада 4 киши бор, 3 таси Telegram'га уланган.", "kind": "grounded",
                   "source_ids": ["DATA"]})
    async with session_scope() as s:
        result = await answer_question(s, await s.get(Employee, hr_id), "nech odam bazaga kiritilgan?", today,
                                       llm=llm)
    assert result.outcome == "answered" and result.text.startswith("Базада 4 киши бор")
    assert result.source.startswith("Манба: HARI маълумотлар базаси")
    prompt = llm.calls[0][1]
    assert '"in_database": 4' in prompt and "Кадрова" not in prompt and "Аълоев" not in prompt


async def test_only_hr_and_owners_are_told_how_to_add_or_list_people(session_scope, staff):
    from hari.answer import abilities

    owner_id, hr_id, emp_id = await team(session_scope, staff)
    async with session_scope() as s:
        owner, hr, emp = [await s.get(Employee, i) for i in (owner_id, hr_id, emp_id)]
    as_text = {who: " ".join(abilities(p)) for who, p in (("owner", owner), ("hr", hr), ("emp", emp))}
    assert "«янги ходим»" in as_text["owner"] and "«ходимлар»" in as_text["hr"] and "HR қил" in as_text["owner"]
    assert "HR қил" not in as_text["hr"]
    assert "«янги ходим»" not in as_text["emp"] and "«ходимлар»" not in as_text["emp"]


async def test_an_employee_sees_no_list_no_card_and_no_company_numbers(session_scope, staff):
    from tests.test_bot_flow import Harness

    await team(session_scope, staff)
    h = Harness()
    for text in ("ходимлар", "/xodimlar", "E-0001", "E-0002 профили", "янги ходим", "аризалар"):
        assert (await h.say(13, text)) == ["Бу амал учун ҳуқуқингиз йўқ."], text
    staff_list = (await h.say(12, "ходимлар"))[0]  # HR does see it
    assert "Аълоев Анвар" in staff_list and "Эгаев Улуғ" in staff_list
