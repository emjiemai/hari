"""An applicant applies through the real dispatcher with a fake Telegram connection."""

from __future__ import annotations

from sqlalchemy import func, select

from hari.apply import evaluate as E
from hari.apply.vacancies import list_open, set_vacancy_status
from hari.db.models import Application
from tests.apply_fixtures import (  # noqa: F401 — fixtures
    APPLICANT, HR, OWNER, ApplyHarness, apply_to_the_end, h_apply, use_ai,
)
from tests.test_bot_flow import btn


async def test_full_application_reaches_owner_and_hr(h_apply, monkeypatch, session_scope):
    h = h_apply
    use_ai(monkeypatch, {"questions": ["Мижоз «қиммат» деса, нима дейсиз?", "Биринчи ойда нима қиласиз?"]},
           {"verdict": "strong", "reason": "Аниқ ва амалий жавоблар.", "answer_notes": ["n1", "n2"]})
    replies = await h.say(APPLICANT, "/start apply_garmin")
    assert "Тилни танланг" in replies[0]
    replies = await apply_to_the_end(h, link="/start apply_garmin")
    assert "3 иш куни" in replies[0]
    assert any("Мижоз «қиммат» деса" in m and "4/5" in m for m in h.to(APPLICANT))
    for tg in (OWNER, HR):
        report = "\n".join(h.to(tg))
        assert "Суҳбатга чақиришга арзийди" in report and "Алиев Вали" in report and "+998901234567" in report
        assert "Сайт: Garmin" in report and "B2B савдо менежери" in report and "Биринчи жавоб" in report
    assert [b for b, _ in h.buttons(OWNER)] == ["Суҳбатга чақириш", "Рад этиш", "Ишга олиш"]
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    assert app.status == "submitted" and app.number == app.number and app.reported_at is not None
    assert app.verdict == "strong" and app.job_origin == "ai" and app.source == "apply_garmin"


async def test_no_ai_still_reports_in_russian(h_apply):
    h = h_apply
    replies = await apply_to_the_end(h, link="/start apply_primus", lang="Русский", consent="Согласен",
                                     vacancy="Менеджер по B2B", skip="Пропустить")
    assert "3 рабочих дней" in replies[0]
    assert any(E.FALLBACK["ru"][0] in m for m in h.to(APPLICANT))
    report = "\n".join(h.to(OWNER))
    assert "HARI баҳоси: AI баҳоси йўқ" in report and "Сайт: Primus" in report and "Тил: Русский" in report


async def test_restart_mid_flow_continues(h_apply):
    h = h_apply
    await h.say(APPLICANT, "/start apply")
    await h.press(APPLICANT, btn(h, APPLICANT, "Ўзбекча"))
    await h.press(APPLICANT, btn(h, APPLICANT, "Розиман"))
    await h.say(APPLICANT, "Алиев Вали")
    await h.say(APPLICANT, "+998 90 123 45 67")
    fresh = ApplyHarness()  # a redeploy: new dispatcher state, same database
    replies = await fresh.say(APPLICANT, "/start apply_garmin")
    assert "Қайси лавозимга" in replies[-1]
    replies = await fresh.say(APPLICANT, "/start")
    assert "Қайси лавозимга" in replies[-1]


async def test_wrong_inputs_keep_the_step(h_apply, session_scope):  # Review Focus 1, 3, 4
    h = h_apply
    await h.say(APPLICANT, "/start apply")
    await h.press(APPLICANT, btn(h, APPLICANT, "Ўзбекча"))
    assert "тугмалардан бирини" in (await h.say(APPLICANT, "салом"))[0]  # text where a button is expected
    await h.press(APPLICANT, btn(h, APPLICANT, "Розиман"))
    assert "ёзма" in (await h.photo(APPLICANT))[0]
    assert "ёзма" in (await h.voice(APPLICANT))[0]
    assert "фамилия ва исмни" in (await h.say(APPLICANT, "Вали"))[0]
    await h.say(APPLICANT, "Алиев Вали")
    assert "ўзингизнинг" in (await h.contact(APPLICANT, "+998901112233", owner=999))[0]
    assert "тушунмадим" in (await h.say(APPLICANT, "қўнғироқ қилинг"))[0]
    replies = await h.contact(APPLICANT, "998901234567")
    assert "+998901234567" in replies[0] and "Қайси лавозимга" in replies[-1]
    async with session_scope() as s:
        b2b = next(v for v in await list_open(s) if v.key == "b2b")
        await set_vacancy_status(s, b2b.id, "closed", actor_id=None)
    replies = await h.press(APPLICANT, btn(h, APPLICANT, "B2B"))
    assert "ёпилди" in replies[0] and not any(t.startswith("B2B") for t, _ in h.buttons(APPLICANT))
    await h.press(APPLICANT, btn(h, APPLICANT, "Бухгалтер"))
    await h.say(APPLICANT, "Тажрибам бор")
    assert "файл ёки фото" in (await h.say(APPLICANT, "резюмем йўқ"))[0]
    await h.document(APPLICANT)
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    assert app.resume_file_id == "document:cv-file" and app.step == "logic" and len(app.logic) == 3
    old = h.buttons(APPLICANT)[0][1]
    await h.press(APPLICANT, old)
    await h.press(APPLICANT, old)  # the same button again changes nothing
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    assert [r["choice"] is not None for r in app.logic] == [True, False, False]


async def test_after_submitting_the_applicant_sees_the_status(h_apply):
    h = h_apply
    await apply_to_the_end(h)
    replies = await h.say(APPLICANT, "Салом, жавоб борми?")
    assert "кўриб чиқилмоқда" in replies[0]
    assert any(text.startswith("Бошқа вакансияга") for text, _ in h.buttons(APPLICANT))
    assert "кўриб чиқилмоқда" in (await h.say(APPLICANT, "/start apply_garmin"))[0]
    replies = await h.press(APPLICANT, btn(h, APPLICANT, "Бошқа вакансияга"))
    assert "Тилни танланг" in replies[0]


async def test_staff_and_strangers(h_apply, session_scope):
    h = h_apply
    assert "ходимисиз" in (await h.say(HR, "/start apply_garmin"))[0]
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Application))).scalar_one() == 0
    replies = await h.say(777, "/start")
    assert "боғланмаган" in replies[0]
    replies = await h.press(777, btn(h, 777, "Ишга ариза"))
    assert "Тилни танланг" in replies[0]
    assert "ишламайди" in (await h.say(778, "/start not-an-invite"))[0]  # invite links are untouched


async def test_declining_consent_saves_nothing(h_apply, session_scope):
    h = h_apply
    await h.say(APPLICANT, "/start apply")
    await h.press(APPLICANT, btn(h, APPLICANT, "Русский"))
    replies = await h.press(APPLICANT, btn(h, APPLICANT, "Нет"))
    assert "ничего не сохранено" in replies[0]
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Application))).scalar_one() == 0
