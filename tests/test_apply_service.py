"""The application state machine without Telegram: drafts, checks, the logic test, numbers and decisions."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from hari.apply.applications import (
    ApplyError, answer_job, answer_logic, can_hire, choose_vacancy, clean_name, clean_phone, create_manual,
    current_job, current_logic, decide, draw_logic, go, logic_score, mark_hired, start_or_resume, submit,
)
from hari.apply.logic_bank import BANK, by_id, option_labels, question_text
from hari.apply.vacancies import list_open, seed_vacancies, set_vacancy_status
from hari.db.models import Application

NOW = datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)  # 12:00 in Tashkent


def test_bank_is_sound():
    assert len(BANK) >= 15 and len({q.qid for q in BANK}) == len(BANK)
    for q in BANK:
        assert set(q.text) == set(q.options) == {"uz_cyrl", "ru"} and 0 <= q.answer < 4
        assert all(len(o) == 4 and len(set(o)) == 4 for o in q.options.values())
    q = BANK[0]
    assert question_text(q, "uz_latn") != question_text(q, "uz_cyrl")  # Latin by transliteration
    assert len(option_labels(q, "ru")) == 4


def test_clean_phone_and_name():
    assert clean_phone("90 123-45-67") == "+998901234567"
    assert clean_phone("+998 (90) 123 45 67") == "+998901234567"
    assert clean_phone("998901234567") == "+998901234567"
    assert clean_phone("12345") is None and clean_phone("call me") is None
    assert clean_name("  Алиев   Вали ") == "Алиев Вали" and clean_name("Вали") is None
    assert clean_name("12 34") is None


async def test_resume_continues_same_draft(session_scope):  # a redeploy loses nothing
    async with session_scope() as s:
        app, created = await start_or_resume(s, 9001, "apply_primus")
        app.full_name = "Алиев Вали"
        go(app, "phone", NOW)
    async with session_scope() as s:
        again, created2 = await start_or_resume(s, 9001, "apply")
    assert created and not created2 and again.id == app.id and again.step == "phone"
    assert again.source == "apply_primus"


def test_logic_answers_once_and_in_order():  # Review Focus 2
    app = Application(logic=[], step_started_at=NOW)
    draw_logic(app, random.Random(1))
    assert len({row["qid"] for row in app.logic}) == 3 and current_logic(app) == 0
    first = by_id(app.logic[0]["qid"])
    assert not answer_logic(app, 1, 0, NOW)  # out of order
    assert not answer_logic(app, 0, 7, NOW)  # no such option
    assert answer_logic(app, 0, first.answer, NOW + timedelta(seconds=42))
    assert not answer_logic(app, 0, (first.answer + 1) % 4, NOW)  # the old button again
    assert logic_score(app) == (1, 3) and app.logic[0]["seconds"] == 42 and current_logic(app) == 1


def test_job_answers_are_clipped_and_in_order():
    app = Application(job=[{"q": "Q1", "a": None}, {"q": "Q2", "a": None}])
    assert current_job(app) == 0 and answer_job(app, "x" * 2000)
    assert len(app.job[0]["a"]) == 1500 and app.job[0]["a"].endswith("…")
    assert answer_job(app, "ok") and current_job(app) is None and not answer_job(app, "more")


async def test_closed_or_duplicate_vacancy_refused(session_scope):  # Review Focus 4
    async with session_scope() as s:
        await seed_vacancies(s)
        first, second = (await list_open(s))[:2]
        await set_vacancy_status(s, first.id, "closed", actor_id=None)
        app, _ = await start_or_resume(s, 9002, "apply")
        with pytest.raises(ApplyError) as err:
            await choose_vacancy(s, app, first.id)
        assert str(err.value) == "vacancy_closed"
        await choose_vacancy(s, app, second.id)
        await submit(s, app, NOW)
        again, created = await start_or_resume(s, 9002, "apply")
        assert created
        with pytest.raises(ApplyError) as err:
            await choose_vacancy(s, again, second.id)
        assert str(err.value) == "duplicate"
        await choose_vacancy(s, again, None)  # a general application is always fine
        assert again.vacancy_id is None


async def test_numbering_and_decisions_first_wins(session_scope, staff):
    async with session_scope() as s:
        boss = await staff(s, "Раҳбаров Бек", "owner", tg=1)
        hr = await staff(s, "Кадрова Зайнаб", "hr", tg=2)
        a, _ = await start_or_resume(s, 9003, "apply")
        await submit(s, a, NOW)
        b, _ = await start_or_resume(s, 9004, "apply")
        await submit(s, b, NOW)
        assert (a.number, b.number) == ("A-2026-11-0001", "A-2026-11-0002")
        assert a.status == "submitted" and a.step == "done" and can_hire(a)
        app_id = a.id
    async with session_scope() as s:
        assert (await decide(s, app_id, boss, "interview")).ok
    async with session_scope() as s:
        assert (await decide(s, app_id, hr, "reject")).ok  # an interview isn't final
    async with session_scope() as s:
        late = await decide(s, app_id, boss, "interview")
        assert not late.ok and late.status == "rejected" and late.decided_by_name == "Кадрова Зайнаб"
        app = await s.get(Application, app_id)
        assert not can_hire(app) and not await mark_hired(s, app_id, boss, employee_id=None)
        with pytest.raises(ValueError):
            await decide(s, app_id, boss, "promote")


async def test_walk_in_applicant(session_scope, staff):
    async with session_scope() as s:
        hr = await staff(s, "Кадрова Зайнаб", "hr", tg=2)
        app = await create_manual(s, actor=hr, full_name="Пиёда Келди", phone="+998901112233",
                                  vacancy_id=None, note="Офисга ўзи келди")
    assert app.source == "manual" and app.status == "submitted" and app.number.startswith("A-")
    assert app.telegram_user_id is None and app.reported_at is not None
