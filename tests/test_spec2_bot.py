"""The 13.11.2026 spec through the real dispatcher: complaints, a wish to leave, resignation, the
interview score, deletion requests, role changes, and the pieces under them (digest, registry,
schema upgrade, budget)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine, inspect, select, text

from hari import budget, digest, scheduler
from hari.config import settings
from hari.db.models import Application, Approval, DigestItem, Employee, HRCase, ResponseItem
from hari.db.session import add_missing_columns
from hari.knowledge import registry
from tests.apply_fixtures import APPLICANT, h_apply, h_apply_done  # noqa: F401 — fixtures
from tests.apply_fixtures import HR as A_HR
from tests.apply_fixtures import OWNER as A_OWNER
from tests.conftest import FakeBot, at_tashkent
from tests.test_bot_flow import Harness, btn

OWNER, HR, BOSS, ME, LAWYER = 701, 702, 703, 704, 705
MONDAY = date(2026, 11, 9)


@pytest.fixture
async def h(session_scope, staff):
    async with session_scope() as s:
        await staff(s, "Эгаев Улуғ", "owner", department="Раҳбарият", position="Директор", tg=OWNER)
        await staff(s, "Кадрова Зайнаб", "hr", department="HR", position="HR масъули", tg=HR)
        boss = await staff(s, "Раҳбаров Бек", "manager", position="Сотув раҳбари", tg=BOSS)
        await staff(s, "Аълоев Анвар", tg=ME, manager=boss)
    return Harness()


async def cases(session_scope) -> list[HRCase]:
    async with session_scope() as s:
        return list((await s.execute(select(HRCase).order_by(HRCase.created_at))).scalars())


# --------------------------------------------------------------------------- complaints (T16)

async def test_complaint_from_the_menu_asks_who_it_is_about_and_avoids_them(h, session_scope):
    await h.say(ME, "Мурожаат")
    replies = await h.press(ME, btn(h, ME, "Шикоят"))
    assert "Шикоят кимга оид?" in replies[0]
    await h.press(ME, btn(h, ME, "Бевосита раҳбарим"))
    replies = await h.say(ME, "Мажлисда қўпол гапирди, 5 ноябрь")
    assert "Мурожаат қабул қилинди: HR-" in replies[-1] and "Кадрова Зайнаб" in replies[-1]
    assert any("Мажлисда қўпол гапирди" in t for t in h.to(HR))
    assert not any("қўпол" in t for t in h.to(BOSS)) and not any("қўпол" in t for t in h.to(OWNER))
    (case,) = await cases(session_scope)
    async with session_scope() as s:
        boss = (await s.execute(select(Employee).where(Employee.telegram_user_id == BOSS))).scalar_one()
        assert case.about_employee_id == boss.id
        item = (await s.execute(select(ResponseItem))).scalar_one()  # HR now owes an answer; delivery confirmed
        assert item.delivery_status == "confirmed" and "нозик мурожаат" in item.title and "Шикоят" not in item.title
    assert await h.say(BOSS, "Иш навбати") == ["Иш навбатингиз бўш."]


async def test_complaint_about_the_owner_never_reaches_an_owner(h, session_scope):
    replies = await h.say(ME, "Директор устидан шикоят қилмоқчиман, у мени ҳақорат қилди")
    assert "шикоят қилинган шахс уни текширмайди" in replies[0]
    replies = await h.press(ME, btn(h, ME, "Мурожаат очиш"))
    assert "Шикоят кимга оид?" in replies[0] and await cases(session_scope) == []  # nothing routed yet
    replies = await h.press(ME, btn(h, ME, "Компания раҳбари"))
    assert "Кадрова Зайнаб" in replies[-1] and "мустақил масъул ҳали тайинланмаган" in replies[-1]
    assert "Мутлақ махфийликни ваъда қила олмайман" in replies[-1]
    (case,) = await cases(session_scope)
    assert case.about_role == "owner" and "ҳақорат" in case.body
    assert not any("ҳақорат" in t for t in h.to(OWNER))
    assert await h.say(OWNER, "Иш навбати") == ["Иш навбатингиз бўш."]
    await scheduler.tick(h.bot, at_tashkent(MONDAY, 17, 40))  # and no digest line about it either
    assert not any("дайжест" in t for t in h.to(OWNER))


# --------------------------------------------------------------------------- leaving (T11)

async def test_saying_i_want_to_leave_opens_one_minimal_hr_signal(h, session_scope):
    words = "Маошим кам, раҳбар билан келиша олмаяпман, ишдан кетмоқчиман"
    await h.say(ME, words)
    (reply,) = h.to(ME)  # one message, nothing to press: the person is told what happened and who sees it
    assert "якуний қарор ёки ариза ҳисобланмайди" in reply and "HR масъулига қисқа хабар бердим" in reply
    assert not h.buttons(ME)
    await h.say(ME, "Ростдан ҳам кетишни ўйлаяпман")  # the same day: no second case
    (case,) = await cases(session_scope)
    assert case.type == "exit_intent" and "Маошим" not in case.body and "раҳбар билан" not in case.body
    hr_card = "\n".join(h.to(HR))
    assert "кетишни ўйлаётганини айтди" in hr_card and "Маошим кам" not in hr_card
    assert h.to(OWNER) == []  # the owner hears through the digest, not at once, and never the conversation
    await scheduler.tick(h.bot, at_tashkent(MONDAY, 17, 31))
    owner_digest = h.to(OWNER)[-1]
    assert "кунлик дайжест" in owner_digest and "Аълоев Анвар" in owner_digest and "якуний қарор эмас" in owner_digest
    assert "Маошим" not in owner_digest and "Иш узлуксизлиги" in owner_digest
    async with session_scope() as s:
        me = (await s.execute(select(Employee).where(Employee.telegram_user_id == ME))).scalar_one()
        assert me.status == "active"


async def test_formal_resignation_tells_hr_and_the_owner_at_once(h, session_scope):
    await h.say(ME, "Мурожаат")
    replies = await h.press(ME, btn(h, ME, "Ишдан бўшаш аризаси"))
    assert "компания раҳбарига қисқа хабар берилади" in replies[0]
    await h.say(ME, "1 декабрдан ишдан бўшамоқчиман, сабаби шахсий")
    (case,) = await cases(session_scope)
    assert case.type == "resignation"
    owner_note = h.to(OWNER)[-1]
    assert "расмий ишдан бўшаш аризаси берди" in owner_note and "сабаби шахсий" not in owner_note
    assert any("сабаби шахсий" in t for t in h.to(HR))  # HR handles the request itself


# --------------------------------------------------------------------------- onboarding wait is tracked

async def test_step_sent_to_the_mentor_becomes_a_tracked_wait(session_scope, staff):
    from hari import onboarding as O

    async with session_scope() as s:
        await staff(s, "Кадрова Зайнаб", "hr", tg=HR)
        mentor = await staff(s, "Устозов Анвар", tg=BOSS)
        new = await staff(s, "Янгиев Ботир", tg=ME, mentor=mentor, start=MONDAY)
        await O.create_plan(s, new, MONDAY)
    h = Harness()
    await h.say(ME, "Режам")
    await h.press(ME, btn(h, ME, "Бажардим"))
    await h.say(ME, "Мураббий билан учрашдим")
    async with session_scope() as s:
        item = (await s.execute(select(ResponseItem))).scalar_one()
        assert item.kind == "step" and item.delivery_status == "confirmed" and item.state == "open"
    await h.press(BOSS, btn(h, BOSS, "Қабул"))
    async with session_scope() as s:
        assert (await s.execute(select(ResponseItem))).scalar_one().state == "resolved"


# --------------------------------------------------------------------------- hiring: score, deletion, hold

async def test_interview_score_is_given_by_a_person_and_completes_the_total(h_apply_done, session_scope):
    h = h_apply_done
    await h.press(A_OWNER, btn(h, A_OWNER, "Суҳбатга"))
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
        app.scorecard = {"rubric_version": "1", "recommendation": "ready", "summary": "", "missing": [], "total": None,
                         "criteria": {"work_sample": {"score": 3, "evidence": "д"},
                                      "interview": {"score": None, "evidence": ""},
                                      "learning": {"score": 2, "evidence": "д"},
                                      "collaboration": {"score": 3, "evidence": "д"}}}
        app_id = app.id
    from hari.bot.ui import AppActCb

    replies = await h.press(A_HR, AppActCb(action="score", app_id=app_id).pack())
    assert "Суҳбатни ўтказган киши қўяди" in replies[0]
    replies = await h.press(A_HR, btn(h, A_HR, "4 —"))
    assert "Тузилмали суҳбат (30%): 4" in replies[0] and "Умумий балл: 78.75 / 100" in replies[0]
    assert "қабул ёки рад чегараси эмас" in replies[0]
    async with session_scope() as s:
        app = await s.get(Application, app_id)
        assert app.scorecard["total"] == 78.75 and app.status == "interview"  # a total changes no status
    await h.press(APPLICANT, AppActCb(action="iv0", app_id=app_id).pack())  # an applicant cannot score
    async with session_scope() as s:
        assert (await s.get(Application, app_id)).scorecard["criteria"]["interview"]["score"] == 4


async def test_deletion_request_is_decided_by_a_person(h_apply_done, session_scope):
    h = h_apply_done
    await h.say(APPLICANT, "салом")  # the status reply carries the button
    replies = await h.press(APPLICANT, btn(h, APPLICANT, "Маълумотимни ўчириш"))
    assert "HR масъули кўриб чиқади" in replies[0]
    assert "ўчиришни сўради" in h.to(A_HR)[-1]
    assert "аллақачон" in (await h.press(APPLICANT, btn(h, APPLICANT, "Маълумотимни ўчириш")))[0]
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
        assert app.full_name == "Алиев Вали" and app.deletion_requested_at is not None  # asked, not yet deleted
    replies = await h.press(A_HR, btn(h, A_HR, "Маълумотни ўчириш"))
    assert "шахсий маълумот ўчирилди" in replies[0] and "ўчирилди" in h.to(APPLICANT)[-1]
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
        assert app.full_name == "[ўчирилган]" and app.phone == "" and app.status == "withdrawn" and app.number


async def test_legal_hold_blocks_the_delete_button(h_apply_done, session_scope, staff):
    h = h_apply_done
    async with session_scope() as s:
        await staff(s, "Юристов Камол", "lawyer", tg=LAWYER)
    await h.say(APPLICANT, "салом")
    await h.press(APPLICANT, btn(h, APPLICANT, "Маълумотимни ўчириш"))
    delete = btn(h, A_HR, "Маълумотни ўчириш")
    replies = await h.press(A_HR, btn(h, A_HR, "Сақлаш"))
    assert "legal hold ўрнатилди" in replies[0] and "юристга хабар берилди" in replies[0]
    assert "legal hold" in h.to(LAWYER)[-1] and "қонуний сабабга кўра сақланмоқда" in h.to(APPLICANT)[-1]
    await h.press(A_OWNER, delete)  # anyone pressing delete afterwards is refused
    assert any("Legal hold ўрнатилган" in a for a in h.session.alerts)
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
        assert app.full_name == "Алиев Вали" and app.legal_hold is True and app.purged_at is None


# --------------------------------------------------------------------------- role change (T19, T20)

async def test_role_change_needs_the_owners_one_time_confirmation(h, session_scope):
    assert await h.say(HR, "/rol E-0004 manager") == ["Бу амал учун ҳуқуқингиз йўқ."]
    replies = await h.say(OWNER, "/rol E-0004 hr")
    assert "Роль ўзгариши лойиҳаси: E-0004 Аълоев Анвар" in replies[0] and "Ходим → HR" in replies[0]
    confirm = btn(h, OWNER, "Тасдиқлаш")
    async with session_scope() as s:
        me = (await s.execute(select(Employee).where(Employee.code == "E-0004"))).scalar_one()
        assert me.role == "employee"  # a draft changes nothing
    await h.press(HR, confirm)  # someone else's press: refused
    assert any("фақат эгаси" in a for a in h.session.alerts)
    replies = await h.press(OWNER, confirm)
    assert "роль — HR" in replies[-1]
    await h.press(OWNER, confirm)  # the same approval again
    assert any("аллақачон ишлатилган" in a for a in h.session.alerts)
    async with session_scope() as s:
        me = (await s.execute(select(Employee).where(Employee.code == "E-0004"))).scalar_one()
        assert me.role == "hr" and (await s.execute(select(Approval))).scalar_one().used_at is not None


# --------------------------------------------------------------------------- the owner's digest

async def test_digest_is_at_most_five_lines_once_and_hides_an_owners_own_matter(session_scope, staff):
    async with session_scope() as s:
        one = await staff(s, "Эгаев Улуғ", "owner", tg=OWNER)
        await staff(s, "Иккинчи Эга", "owner", tg=BOSS)
        for n in range(7):
            await digest.add(s, f"k{n}", "x", f"Факт {n}", impact="таъсир", responsible="HR", due="бугун", priority=n)
        await digest.add(s, "about-one", "x", "Биринчи эга ҳақида", priority=0, about_employee_id=one.id)
    bot = FakeBot()
    await scheduler.tick(bot, at_tashkent(MONDAY, 17, 0))
    assert bot.sent == []  # not before 17:30
    await scheduler.tick(bot, at_tashkent(MONDAY, 17, 30))
    await scheduler.tick(bot, at_tashkent(MONDAY, 17, 45))  # once a day
    assert sorted(chat for chat, _ in bot.sent) == [OWNER, BOSS]
    mine = next(t for chat, t in bot.sent if chat == OWNER)
    other = next(t for chat, t in bot.sent if chat == BOSS)
    assert "Биринчи эга ҳақида" not in mine and "Биринчи эга ҳақида" in other
    assert mine.count("Таъсир:") == 5 and "Факт 0" in mine and "Факт 5" not in mine
    assert "Тизим ҳолати:" in mine and "LLM_DAILY_CALL_LIMIT" in mine  # undecided settings are shown, not guessed
    await scheduler.tick(bot, at_tashkent(date(2026, 11, 10), 17, 30))  # what did not fit comes the next day
    tomorrow = [t for chat, t in bot.sent if chat == OWNER][-1]
    assert "Факт 5" in tomorrow and "Факт 0" not in tomorrow
    await scheduler.tick(bot, at_tashkent(date(2026, 11, 11), 17, 30))  # nothing left: no empty digest
    assert len([1 for chat, _ in bot.sent if chat == OWNER]) == 2
    async with session_scope() as s:
        assert all(i.sent_on for i in (await s.execute(select(DigestItem))).scalars())


async def test_weekly_report_goes_to_hr_and_owner_on_friday_afternoon(session_scope, staff):
    async with session_scope() as s:
        await staff(s, "Эгаев Улуғ", "owner", tg=OWNER)
        await staff(s, "Кадрова Зайнаб", "hr", tg=HR)
        await staff(s, "Аълоев Анвар", tg=ME)
    bot = FakeBot()
    await scheduler.tick(bot, at_tashkent(date(2026, 11, 12), 16, 30))  # Thursday
    await scheduler.tick(bot, at_tashkent(date(2026, 11, 13), 15, 0))  # Friday, too early
    assert bot.sent == []
    await scheduler.tick(bot, at_tashkent(date(2026, 11, 13), 16, 0))
    await scheduler.tick(bot, at_tashkent(date(2026, 11, 13), 16, 30))
    assert sorted(chat for chat, _ in bot.sent) == [OWNER, HR]
    assert "ҳафталик ҳисобот" in bot.sent[0][1] and "шахсий баҳоси эмас" in bot.sent[0][1]


# --------------------------------------------------------------------------- registry, schema, budget, modes

def test_source_registry_is_valid_and_nothing_is_usable_without_recorded_rights(tmp_path):
    sources = registry.load()
    assert {"S01", "S07", "S08", "S26", "B01", "B08"} <= set(sources) and len(sources) == 36
    assert not any(s.usable for s in sources.values())  # links only until a person records the rights
    assert sources["S07"].canonical_url == "https://lex.uz/docs/-6257288" and sources["S07"].review == "daily"
    entry = ("source_id: X1, title: T, publisher: P, kind: k, canonical_url: 'https://example.org/a', "
             "jurisdiction: UZ, language: uz, review: monthly")
    bad = tmp_path / "r.yaml"
    for extra, error in ((", status: active", "RIGHTS_UNKNOWN"),
                         (", status: active, rights: {index: allowed, train: false}", "APPROVAL_REQUIRED"),
                         (", rights: {index: allowed, train: true}", "train must be false")):
        bad.write_text("sources:\n  - {" + entry + extra + "}\n", encoding="utf-8")
        with pytest.raises(ValueError, match=error):
            registry.load(bad)
    bad.write_text("sources:\n  - {" + entry + ", status: active, approved_by: Зайнаб, "
                   "rights: {index: allowed, train: false}}\n", encoding="utf-8")
    assert registry.load(bad)["X1"].usable


def test_new_columns_are_added_to_a_database_made_before_them(tmp_path):
    from hari.db.models import Base

    new = {"applications": ["scorecard", "decision_basis", "deletion_requested_at", "legal_hold", "purged_at"],
           "hr_cases": ["about_role"],
           "knowledge_documents": ["source_type", "rights_index", "publisher", "canonical_url", "jurisdiction"]}
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        Base.metadata.create_all(conn)
        for table, columns in new.items():  # back to the tables as they were before this spec
            for column in columns:
                conn.execute(text(f'ALTER TABLE {table} DROP COLUMN {column}'))
        conn.execute(text("DROP TABLE approvals"))  # a table that does not exist yet is left to create_all
        conn.execute(text("INSERT INTO vacancies (id, key, title, title_ru, title_en, department, description, "
                          "look_for, question_1, question_2, sort_order, status, created_at, updated_at) VALUES "
                          "('v1', 'b2b', 'B2B', '', '', '', '', '', '', '', 1, 'open', '2026-11-01', '2026-11-01')"))
        added = add_missing_columns(conn)
        assert sorted(added) == sorted(f"{t}.{c}" for t, cols in new.items() for c in cols)
        assert add_missing_columns(conn) == []  # a second start changes nothing
        cols = {c["name"] for c in inspect(conn).get_columns("applications")}
        assert set(new["applications"]) <= cols
        assert conn.execute(text("SELECT title FROM vacancies")).scalar_one() == "B2B"  # old rows untouched


def test_budget_breaker_opens_after_failures_and_closes_on_success(monkeypatch):
    monkeypatch.setattr(settings, "llm_breaker_failures", 3)
    now = at_tashkent(MONDAY, 10)
    state = budget.BudgetState()
    assert state.refusal(now) is None  # test stage, no limit set: allowed (and reported as undecided)
    monkeypatch.setattr(settings, "llm_daily_call_limit", 100)
    for _ in range(3):
        state.failed(now)
    assert state.refusal(now) == "circuit_open"
    assert state.refusal(at_tashkent(MONDAY, 10, 11)) is None  # ten minutes later it tries again
    state.spent(now)
    monkeypatch.setattr(settings, "llm_daily_call_limit", 1)
    assert state.refusal(at_tashkent(MONDAY, 11)) == "daily_limit"
    assert state.refusal(at_tashkent(date(2026, 11, 10), 9)) is None  # a new day, a new count


async def test_live_mode_refuses_documents_nobody_real_approved(docs, session_scope, monkeypatch):
    from hari.knowledge.loader import load_directory

    (docs / "SAMPLE.md").write_text(
        "---\ndoc_id: SAMPLE\ntitle: Намуна\nversion: \"1\"\nstatus: approved\nclassification: internal\n"
        "approved_by: \"НАМУНА — ҳали тасдиқланмаган\"\n---\n## A\n\nматн\n", encoding="utf-8")
    (docs / "REAL.md").write_text(
        "---\ndoc_id: REAL\ntitle: Ҳақиқий\nversion: \"1\"\nstatus: approved\nclassification: internal\n"
        "approved_by: Зайнаб\nsource_type: company_policy\n---\n## A\n\nматн\n", encoding="utf-8")
    async with session_scope() as s:
        first = await load_directory(s, docs)
        assert "SAMPLE" in first.loaded  # the test stage runs on sample texts, and says so in the system state
        assert any("Намуна" in line for line in await digest.system_status(s, at_tashkent(MONDAY, 10)))
    monkeypatch.setattr(settings, "hari_mode", "live")
    async with session_scope() as s:
        live = await load_directory(s, docs)
        refused = dict(live.refused)
        assert "APPROVAL_REQUIRED" in refused["SAMPLE.md"] and "SAMPLE" in live.retired
        assert "REAL" in live.unchanged
