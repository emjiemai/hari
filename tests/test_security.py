"""Data protection: encrypted text at rest, no cross-person access, nothing personal
in logs, masking before the AI provider, and requests that carry only what the person chose to send."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select, text

from hari import cases as C
from hari import crypto, egress, memory
from hari.config import settings
from hari.db import session as db
from hari.db.models import ChatTurn, Employee, HRCase
from hari.employees import bind_telegram, make_invite
from hari.worktime import now_utc
from tests.test_bot_flow import HR, Harness, h  # noqa: F401
from tests.test_followups import EMP, ScriptedLLM, kb  # noqa: F401

KEY = "test-only-key-not-a-real-secret"


@pytest.fixture
def key(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "data_encryption_key", SecretStr(KEY))


# --------------------------------------------------------------------------- encryption at rest

async def test_chat_and_case_text_are_unreadable_in_the_raw_database(key, session_scope, staff):
    async with session_scope() as s:
        emp = await staff(s, "Махфий Ходим", tg=901)
        await memory.remember(s, emp, "Men homiladorman, ishga kech qolaman", "Тушунарли.", now_utc())
        await C.open_case(s, emp, "support", "Uyda zo'ravonlik bor, yordam kerak")
    async with session_scope() as s:
        raw = (await s.execute(text("SELECT question FROM chat_turns"))).scalar_one()
        body = (await s.execute(text("SELECT body FROM hr_cases"))).scalar_one()
        assert raw.startswith(crypto.PREFIX) and "homilador" not in raw
        assert body.startswith(crypto.PREFIX) and "zo'ravonlik" not in body
        # ...and the bot itself reads them as written
        assert (await s.execute(select(ChatTurn))).scalar_one().question.startswith("Men homiladorman")
        assert "zo'ravonlik" in (await s.execute(select(HRCase))).scalar_one().body


async def test_rows_from_before_the_key_stay_readable_and_a_wrong_key_breaks_nothing(session_scope, staff,
                                                                                   monkeypatch):
    from pydantic import SecretStr

    async with session_scope() as s:  # no key yet: plain text, as before this release
        emp = await staff(s, "Эски Ходим", tg=902)
        await memory.remember(s, emp, "eski xabar", "eski javob", now_utc() - timedelta(minutes=5))
    monkeypatch.setattr(settings, "data_encryption_key", SecretStr(KEY))
    async with session_scope() as s:
        emp = (await s.execute(select(Employee).where(Employee.telegram_user_id == 902))).scalar_one()
        await memory.remember(s, emp, "yangi xabar", "yangi javob", now_utc())
        assert [t[0] for t in await memory.recent(s, emp, now_utc())] == ["eski xabar", "yangi xabar"]
    monkeypatch.setattr(settings, "data_encryption_key", SecretStr("a-different-key"))
    async with session_scope() as s:
        emp = (await s.execute(select(Employee).where(Employee.telegram_user_id == 902))).scalar_one()
        assert [t[0] for t in await memory.recent(s, emp, now_utc())] == ["eski xabar", crypto.UNREADABLE]


def test_missing_key_is_reported_to_the_owner():
    assert "DATA_ENCRYPTION_KEY" in settings.missing_config()


# --------------------------------------------------------------------------- rights checked again

async def test_another_hr_cannot_take_or_answer_a_sensitive_case_assigned_elsewhere(session_scope, staff):
    async with session_scope() as s:
        hr1 = await staff(s, "Биринчи HR", "hr", department="HR", tg=911)
        hr2 = await staff(s, "Иккинчи HR", "hr", department="HR", tg=912)
        emp = await staff(s, "Оддий Ходим", tg=913)
        case, _ = await C.open_case(s, emp, "support", "shaxsiy muammo")
        holder, other = (hr1, hr2) if case.assignee_id == hr1.id else (hr2, hr1)
        assert case not in await C.open_for_handler(s, other)
        for action in (lambda: C.take(s, other, case), lambda: C.answer(s, other, case, "x", close=True)):
            with pytest.raises(C.CaseError):
                await action()
        assert case.assignee_id == holder.id and case.decision is None


async def test_hr_cannot_create_an_owner_by_typing_the_role(h):  # noqa: F811
    await h.say(HR, "Янги ходим")
    await h.say(HR, "Ёлғонов Эга")
    await h.say(HR, "HR")
    replies = await h.say(HR, "Директор")
    assert "Эгаси" not in replies[0]  # not offered to HR
    assert "Тушунмадим" in (await h.say(HR, "эгаси"))[0]  # and not taken when typed
    assert "Тушунмадим" in (await h.say(HR, "owner"))[0]
    await h.say(HR, "бекор")
    async with db.session_scope() as s:
        assert (await s.execute(select(Employee).where(Employee.role == "owner"))).first() is None


async def test_a_new_telegram_account_on_a_card_does_not_get_the_old_chat(session_scope, staff):
    async with session_scope() as s:
        emp = await staff(s, "Қайта Боғланган", tg=921)
        await memory.remember(s, emp, "shaxsiy gap", "javob", now_utc())
        token = await make_invite(s, emp, created_by=None)
        await bind_telegram(s, token, 921)  # the same person again: memory stays
        assert await memory.recent(s, emp, now_utc())
        token = await make_invite(s, emp, created_by=None)
        await bind_telegram(s, token, 922)  # someone else's account: memory is wiped
        assert await memory.recent(s, emp, now_utc()) == []


# --------------------------------------------------------------------------- what leaves the server

def test_logs_never_carry_query_values():
    assert db._engine.sync_engine.hide_parameters


@pytest.mark.parametrize("message,secret", [
    ("Aliyev Valiga aytdim", "Vali"),  # Latin spelling of a Cyrillic name, with an ending
    ("Алиев Валининг телефони", "Валининг"),
    ("Каримова Нодира aka bilan janjal", "Нодира"),
    ("telefonim 90 1234567", "1234567"),
    ("tel: 90.123.45.67", "123.45.67"),
    ("8 90 123 45 67 ga qo'ng'iroq qiling", "123 45 67"),
    ("pasport АА1234567", "1234567"),
    ("pasport aa 1234567", "1234567"),
    ("JShShIR 3010 1990 1234 56", "1234 56"),
    ("tug'ilgan sanam 12.05.1990", "12.05.1990"),
])
def test_identifiers_are_masked_before_the_ai(message, secret):
    masked = egress.scrub(message, known=("Алиев Вали", "Каримова Нодира"))
    assert secret not in masked and egress.MASK in masked


def test_masking_happens_before_the_text_is_cut():
    long = "x" * 1995 + " +998 90 123 45 67"
    assert "123" not in egress.scrub(long)[:2000]


async def test_a_colleagues_name_does_not_reach_the_ai(h, kb, session_scope, staff):  # noqa: F811
    async with session_scope() as s:
        await staff(s, "Каримова Нодира", tg=931)
    await h.say(EMP, "Kasalman, Karimova Nodiraga aytib qo'ying")
    assert "Nodira" not in ScriptedLLM.calls[-1] and "Нодира" not in ScriptedLLM.calls[-1]


async def test_a_request_never_carries_an_old_message(h, kb, session_scope):  # noqa: F811
    await h.say(EMP, "Kasalman, xotinim bilan ajrashyapman")
    async with session_scope() as s:
        for turn in (await s.execute(select(ChatTurn))).scalars():
            turn.at = now_utc() - timedelta(days=2)
    await h.say(EMP, "ha tayyorlab yubor")  # the model offers a leave request
    await h.say(EMP, "tasdiqlayman")
    assert "ajrashyapman" not in h.to(HR)[-1]


def test_ai_provider_is_asked_not_to_keep_prompts():
    assert settings.llm_deny_data_collection
