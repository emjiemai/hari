"""People are named, never coded: commands by a person's name («Валини рад эт», «Анварга жавоб: …»),
everything at once («ҳаммасини рад эт / ёп»), and a name fixed by writing — all without AI."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from hari import cases as C
from hari.db.models import Application, Employee, HRCase
from hari.translit import cyrillic_to_latin as lat
from hari.worktime import now_utc
from tests.test_bot_flow import Harness

OWNER, HR, EMP = 821, 822, 823


@pytest.fixture
async def h(session_scope, staff):
    async with session_scope() as s:
        await staff(s, "Эгаев Улуғ", "owner", department="Раҳбарият", position="Директор", tg=OWNER)
        await staff(s, "Кадрова Зайнаб", "hr", department="HR", position="HR масъули", tg=HR)
        emp = await staff(s, "Аълоев Анвар", tg=EMP)
        await C.open_case(s, emp, "certificate", "Банк учун маълумотнома")
        for i, name in enumerate(("Алиев Вали", "Каримов Бобур"), start=1):
            s.add(Application(id=f"ap{i}", number=f"A-2026-11-000{i}", full_name=name, status="submitted",
                              submitted_at=now_utc(), reported_at=now_utc(), telegram_user_id=9000 + i,
                              language="uz_cyrl"))
    return Harness()


async def statuses(session_scope) -> dict[str, str]:
    async with session_scope() as s:
        return {a.full_name: a.status for a in (await s.execute(select(Application))).scalars()}


async def test_reject_by_name_without_any_number(h, session_scope):
    replies = await h.say(OWNER, "Валини рад эт: тажрибаси бу ишга мос эмас")
    assert "Тасдиқлайсизми?" in replies[0] and "Алиев Вали" in replies[0] and "A-2026" not in replies[0]
    await h.say(OWNER, "ҳа")
    assert (await statuses(session_scope)) == {"Алиев Вали": "rejected", "Каримов Бобур": "submitted"}
    assert "бошқа номзодни танладик" in h.to(9001)[-1]


async def test_reject_all_at_once_is_asked_once_and_done(h, session_scope):
    replies = await h.say(OWNER, "hammasini bekor qil, rad et")
    assert lat("2 та аризани рад этаман") in replies[0] and "Aliyev Vali" in replies[0]  # asked in Latin: Latin back
    assert "sabab" not in replies[0].lower()  # no reason demanded: one «ҳа» is enough
    assert (await statuses(session_scope)) == {"Алиев Вали": "submitted", "Каримов Бобур": "submitted"}  # not yet
    replies = await h.say(OWNER, "ha")
    assert "2 та ариза рад этилди" in replies[-1] or lat("2 та ариза рад этилди") in replies[-1]
    assert set((await statuses(session_scope)).values()) == {"rejected"}
    assert "бошқа номзодни танладик" in h.to(9001)[-1]


async def test_a_reason_written_with_it_is_kept(h, session_scope):
    await h.say(OWNER, "барча аризаларни рад эт: Синов аризалари")
    await h.say(OWNER, "ҳа")
    async with session_scope() as s:
        assert {a.decision_basis for a in (await s.execute(select(Application))).scalars()} == {"Синов аризалари"}


async def test_delete_all_applications_removes_them(h, session_scope):
    replies = await h.say(OWNER, "hamma arizalarni o'chir")
    assert lat("2 та аризани ўчираман") in replies[0]
    await h.say(OWNER, "ha")
    async with session_scope() as s:
        apps = list((await s.execute(select(Application))).scalars())
    assert all(a.purged_at is not None and a.full_name == "[ўчирилган]" for a in apps)
    assert "бошқа номзодни танладик" in h.to(9001)[-1]  # they were still waiting: told before the data went
    assert "Ҳозир бундай нарса йўқ" in (await h.say(OWNER, "ҳаммасини ўчир"))[0]


async def test_no_to_the_question_changes_nothing(h, session_scope):
    await h.say(OWNER, "удали все заявки")
    assert "Бекор" in (await h.say(OWNER, "йўқ"))[0]
    assert set((await statuses(session_scope)).values()) == {"submitted"}


async def test_close_all_requests_and_answer_by_name(h, session_scope):
    replies = await h.say(HR, "Анварга жавоб: Маълумотнома эртага тайёр бўлади")
    assert "Аълоев Анвар" not in replies[0] or "HR-" not in replies[0]
    assert "ҳал бўлдими" in replies[0]
    await h.say(HR, "очиқ")  # answered, still open
    assert "эртага тайёр" in h.to(EMP)[-1] and "HR-" not in h.to(EMP)[-1]
    replies = await h.say(HR, "барча мурожаатларни ёп: Маълумотнома берилди")
    assert "1 та мурожаатни жавоб бериб ёпаман" in replies[0] and "Аълоев Анвар" in replies[0]
    await h.say(HR, "ҳа")
    async with session_scope() as s:
        assert {c.status for c in (await s.execute(select(HRCase))).scalars()} == {"closed"}
    assert "Маълумотнома берилди" in h.to(EMP)[-1]


async def test_an_employee_saying_all_is_not_a_bulk_command(h, session_scope):
    await h.say(EMP, "ҳаммасини ўчир")
    assert set((await statuses(session_scope)).values()) == {"submitted"}


async def test_a_name_is_fixed_by_writing(h, session_scope):
    replies = await h.say(OWNER, "Анварнинг исми: Аълоев Анвар Ботирович")
    assert "Тасдиқлайсизми?" in replies[0]
    await h.say(OWNER, "ҳа")
    async with session_scope() as s:
        assert (await s.execute(select(Employee).where(Employee.telegram_user_id == EMP))).scalar_one().full_name \
            == "Аълоев Анвар Ботирович"


@pytest.mark.parametrize("text", ["profilim", "Профилимни кўрсат", "mening ma'lumotlarim", "мен ҳақимда нима биласан",
                                  "мой профиль", "обо мне", "profil", "менинг карточкам", "men kimman"])
async def test_every_way_of_asking_shows_ones_own_profile_for_every_role(h, text):
    for tg, name, other in ((EMP, "Аълоев Анвар", "Кадрова"), (HR, "Кадрова Зайнаб", "Аълоев"),
                            (OWNER, "Эгаев Улуғ", "Кадрова")):
        reply = (await h.say(tg, text))[-1]
        mine = lat(name) if any("a" <= ch <= "z" for ch in text.lower()) else name
        assert mine in reply and other not in reply and lat(other) not in reply, (tg, text, reply[:80])


async def test_someone_elses_card_and_a_fix_request_are_not_ones_own_profile(h, session_scope):
    assert "Аълоев Анвар" in (await h.say(HR, "Анвар профили"))[0]  # HR asking about someone else
    await h.say(EMP, "профилимда хато бор: лавозимим нотўғри ёзилган")  # a request to HR, opened at once
    assert "Профилимдаги хатони тузатиш" in h.to(HR)[-1] and "лавозимим нотўғри" in h.to(HR)[-1]


async def test_accept_all_step_results_at_once(h, session_scope):
    from hari import onboarding as O
    from hari.db.models import OnboardingStep
    from hari.worktime import today_local

    async with session_scope() as s:
        emp = (await s.execute(select(Employee).where(Employee.telegram_user_id == EMP))).scalar_one()
        plan = await O.create_plan(s, emp, today_local(), actor_id=None)
        await s.refresh(plan, ["steps"])
        tasks = [st for st in plan.steps if st.kind != "read"][:2]
        for st in tasks:
            await O.submit(s, emp, st.id, "бажардим, натижа тайёр", None)
    replies = await h.say(HR, "ҳаммасини қабул қил")
    assert f"{len(tasks)} та мослашув натижасини қабул қиламан" in replies[0] and "Аълоев Анвар" in replies[0]
    await h.say(HR, "ҳа")
    async with session_scope() as s:
        assert {(await s.get(OnboardingStep, st.id)).status for st in tasks} == {"accepted"}
    assert any("қабул қилинди" in t for t in h.to(EMP))
