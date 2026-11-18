"""An applicant applies through the real dispatcher with a fake Telegram connection, typing every answer."""

from __future__ import annotations

from sqlalchemy import func, select

from hari.apply import evaluate as E
from hari.apply.vacancies import list_open, set_vacancy_status
from hari.db.models import Application
from tests.apply_fixtures import (  # noqa: F401 — fixtures
    APPLICANT, HR, OWNER, ApplyHarness, apply_to_the_end, h_apply, use_ai, vacancy_number,
)
from tests.test_bot_flow import no_buttons


async def test_full_application_reaches_owner_and_hr(h_apply, monkeypatch, session_scope):
    h = h_apply
    use_ai(monkeypatch, {"questions": ["Мижоз «қиммат» деса, нима дейсиз?", "Биринчи ойда нима қиласиз?"]},
           {"criteria": {"work_sample": {"score": 3, "evidence": "Нархни қиймат билан тушунтирди"},
                         "learning": {"score": None, "evidence": ""},
                         "collaboration": {"score": 3, "evidence": "Аниқ ва ҳурматли ёзди"}},
            "recommendation": "ready_for_human_review", "summary": "Аниқ ва амалий жавоблар.",
            "missing": ["Янги маълумотдан кейин ечимни тузатиш мисоли"], "answer_notes": ["n1", "n2"]})
    replies = await h.say(APPLICANT, "/start apply_garmin")
    assert "Тилни танланг" in replies[0] and "1. Ўзбекча (кирилл)" in replies[0]
    replies = await apply_to_the_end(h, link="/start apply_garmin")
    assert "3 иш куни" in replies[0]
    assert any("Мижоз «қиммат» деса" in m and "4/5" in m for m in h.to(APPLICANT))
    assert any("А) " in m and "ҳарфини ёзинг" in m for m in h.to(APPLICANT))  # logic options are lettered
    for tg in (OWNER, HR):
        report = "\n".join(h.to(tg))
        assert "Инсон кўриб чиқишига тайёр" in report and "Алиев Вали" in report and "+998901234567" in report
        assert "Касбий иш намунаси (40%): 3" in report and "Ўрганиш ва муаммо ечиш (15%): номаълум" in report
        assert "Умумий балл: номаълум" in report  # the interview has no score yet: no total, and no zero
        assert "Сайт: Garmin" in report and "B2B савдо менежери" in report and "Биринчи жавоб" in report
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    assert f"«{app.number} ни суҳбатга чақир»" in h.to(OWNER)[-1] and f"«{app.number} ни ишга ол»" in h.to(OWNER)[-1]
    assert app.status == "submitted" and app.reported_at is not None
    assert app.verdict == "ready" and app.job_origin == "ai" and app.source == "apply_garmin"
    assert app.scorecard["total"] is None and app.scorecard["criteria"]["interview"]["score"] is None
    no_buttons(h)


async def test_no_ai_still_reports_in_russian(h_apply):
    h = h_apply
    replies = await apply_to_the_end(h, link="/start apply_primus", lang="русский", consent="да",
                                     vacancy="Менеджер по B2B", skip="нет")
    assert "3 рабочих дней" in replies[0]
    assert any(E.FALLBACK["ru"][0] in m for m in h.to(APPLICANT))
    report = "\n".join(h.to(OWNER))
    assert "HARI тавсияси: AI баҳоси йўқ" in report and "Сайт: Primus" in report and "Тил: Русский" in report


async def test_latin_choice_by_word(h_apply, session_scope):
    h = h_apply
    await h.say(APPLICANT, "/start apply")
    replies = await h.say(APPLICANT, "lotin")
    assert "Rozimisiz?" in replies[0]
    async with session_scope() as s:
        assert (await s.execute(select(Application))).scalar_one().language == "uz_latn"


async def test_restart_mid_flow_continues(h_apply):
    h = h_apply
    await h.say(APPLICANT, "/start apply")
    await h.say(APPLICANT, "1")
    await h.say(APPLICANT, "розиман")
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
    await h.say(APPLICANT, "1")
    assert "«ҳа» ёки «йўқ»" in (await h.say(APPLICANT, "салом"))[0]  # neither yes nor no: asked again
    await h.say(APPLICANT, "ҳа")
    assert "ёзма" in (await h.photo(APPLICANT))[0]
    assert "ёзма" in (await h.voice(APPLICANT))[0]
    assert "фамилия ва исмни" in (await h.say(APPLICANT, "Вали"))[0]
    await h.say(APPLICANT, "Алиев Вали")
    assert "ўзингизнинг" in (await h.contact(APPLICANT, "+998901112233", owner=999))[0]
    assert "тушунмадим" in (await h.say(APPLICANT, "қўнғироқ қилинг"))[0]
    replies = await h.contact(APPLICANT, "998901234567")  # a shared contact still works
    assert "+998901234567" in replies[0] and "Қайси лавозимга" in replies[-1]
    assert "рўйхатдан рақамни" in (await h.say(APPLICANT, "99"))[0]
    b2b = vacancy_number(h, APPLICANT, "B2B")
    async with session_scope() as s:
        vac = next(v for v in await list_open(s) if v.key == "b2b")
        await set_vacancy_status(s, vac.id, "closed", actor_id=None)
    replies = await h.say(APPLICANT, b2b)
    assert "ёпилди" in replies[0] and "B2B" not in replies[-1]
    await h.say(APPLICANT, vacancy_number(h, APPLICANT, "Бухгалтер"))
    await h.say(APPLICANT, "Тажрибам бор")
    assert "файл ёки фото" in (await h.say(APPLICANT, "резюмени кейин"))[0]
    await h.document(APPLICANT)
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    assert app.resume_file_id == "document:cv-file" and app.step == "logic" and len(app.logic) == 3
    assert "ҳарфини" in (await h.say(APPLICANT, "7"))[0]  # options are letters, not numbers
    await h.say(APPLICANT, "б")
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    assert [r["choice"] for r in app.logic] == [1, None, None]


async def test_after_submitting_the_applicant_sees_the_status(h_apply):
    h = h_apply
    await apply_to_the_end(h)
    replies = await h.say(APPLICANT, "Салом, жавоб борми?")
    assert "кўриб чиқилмоқда" in replies[0] and "«янги ариза»" in replies[0]
    assert "кўриб чиқилмоқда" in (await h.say(APPLICANT, "/start apply_garmin"))[0]
    replies = await h.say(APPLICANT, "янги ариза")
    assert "Тилни танланг" in replies[0]


async def test_staff_and_strangers(h_apply, session_scope):
    h = h_apply
    assert "ходимисиз" in (await h.say(HR, "/start apply_garmin"))[0]
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Application))).scalar_one() == 0
    replies = await h.say(777, "/start")
    assert "боғланмаган" in replies[0] and "«ариза»" in replies[0]
    assert "боғланмаган" in (await h.say(777, "салом"))[0]  # no card, no rights
    replies = await h.say(777, "ариза бермоқчиман")
    assert "Тилни танланг" in replies[0]
    assert "ишламайди" in (await h.say(778, "/start not-an-invite"))[0]  # invite links are untouched


async def test_declining_consent_saves_nothing(h_apply, session_scope):
    h = h_apply
    await h.say(APPLICANT, "/start apply")
    await h.say(APPLICANT, "3")
    replies = await h.say(APPLICANT, "нет")
    assert "ничего не сохранено" in replies[0]
    async with session_scope() as s:
        assert (await s.execute(select(func.count()).select_from(Application))).scalar_one() == 0
