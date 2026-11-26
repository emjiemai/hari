"""Follow-up replies: HARI reads "yes, send it" and "I told you…" against what was just said, and a "yes"
to the request it offered opens that request instead of asking "which request?" again."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import select

from hari import answer as A
from hari.config import settings
from hari.guard import is_consent
from hari.knowledge.loader import load_directory
from hari.llm import LLMReply
from hari.translit import cyrillic_to_latin as lat
from tests.conftest import write_doc
from tests.test_bot_flow import HR, MENTOR, Harness  # noqa: F401  (h fixture is defined there)
from tests.test_bot_flow import h  # noqa: F401

EMP = MENTOR  # an ordinary employee with no manager: a leave request goes to HR
FRIEND = 505  # another employee of the same company


class ScriptedLLM:
    """Answers by what the current QUESTION says; records every prompt it got."""

    calls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200,
                            temperature: float = 0.1) -> LLMReply:
        ScriptedLLM.calls.append(user)
        question = re.search(r"<<<QUESTION\n(.*)\nQUESTION>>>", user, re.DOTALL).group(1)
        example = json.loads(re.search(r"<<<EXAMPLES\n(.*)\nEXAMPLES>>>", user, re.DOTALL).group(1))
        ex_id = example[0]["items"][0]["id"]
        if "Kasalman" in question:
            return LLMReply({"answer": "Тузалиб кетинг. Бугунги ишларингизни кимга топширишни раҳбарингиз билан "
                                       "келишинг.", "kind": "grounded", "source_ids": [ex_id], "offer": None}, "fake")
        # a follow-up: only answerable from RECENT CONVERSATION
        assert "<<<HISTORY" in user and "Kasalman" in user, "the earlier turn did not reach the model"
        return LLMReply({"answer": "Раҳбарингизга хабар бериш учун мурожаат очиш мумкин.",
                         "kind": "grounded", "source_ids": [ex_id], "offer": "leave"}, "fake")


@pytest.fixture
async def kb(docs, session_scope, monkeypatch):
    write_doc(docs, "HARI-QA-90", "Тузалиб кетинг. Ишга чиқа олмаслигингиз ҳақида мурожаат очинг.",
              title="НАМУНА: Савол-жавоб — Таътил ва касаллик", section="Касалман, ишга келолмайман.")
    async with session_scope() as session:
        await load_directory(session, docs)
    ScriptedLLM.calls = []
    monkeypatch.setattr(A, "LLMClient", ScriptedLLM)


async def test_screenshot_conversation_ends_in_a_request_not_a_question(h, kb):  # noqa: F811
    first = await h.say(EMP, "Kasalman, ishga kelolmayman")
    assert lat("Тузалиб кетинг") in first[0]  # written in Latin: answered in Latin, like a person would

    second = await h.say(EMP, "ha tayyorlab yubor")
    assert lat("мурожаат очиш мумкин") in second[0] and lat("Айнан қайси") not in second[0]
    assert lat("Мурожаат очайми?") in second[0]  # how to open it: by writing

    calls_before = len(ScriptedLLM.calls)
    third = await h.say(EMP, "tasdiqlayman")
    assert len(ScriptedLLM.calls) == calls_before  # a "yes" to the offer is not another AI question
    assert lat("Мурожаатингиз қабул қилинди") in third[-1]
    # HR sees what the person actually said, not only "send it"
    assert "Kasalman" in h.to(HR)[-1] and "ha tayyorlab yubor" in h.to(HR)[-1]


async def test_yes_without_an_offer_goes_to_the_model_with_context(h, kb):  # noqa: F811
    await h.say(EMP, "Kasalman, ishga kelolmayman")
    replies = await h.say(EMP, "ha")  # nothing was offered: nothing is opened on a bare "yes"
    assert "Мурожаат қабул қилинди" not in " ".join(replies)
    assert "<<<HISTORY" in ScriptedLLM.calls[-1]


async def test_sensitive_rule_topic_is_not_carried_to_the_model(h, kb):  # noqa: F811
    await h.say(EMP, "Kasalman, ishga kelolmayman")
    await h.say(EMP, "раҳбаримдан шикоят қилмоқчиман")  # fixed reply, never sent to AI
    ScriptedLLM.calls.clear()
    await h.say(EMP, "Kasalman ertaga ham")
    assert "шикоят" not in ScriptedLLM.calls[-1]
    assert "Kasalman, ishga kelolmayman" in ScriptedLLM.calls[-1]  # the rest of the chat is still remembered


async def test_memory_survives_a_restart(h, kb):  # noqa: F811
    await h.say(EMP, "Kasalman, ishga kelolmayman")
    restarted = Harness()  # a redeploy: the bot's in-memory state is gone, the database is not
    await restarted.say(EMP, "boshim ogridi deb aytdimku")
    assert "Kasalman, ishga kelolmayman" in ScriptedLLM.calls[-1]


async def test_one_person_never_sees_another_persons_chat(h, kb, session_scope, staff):  # noqa: F811
    async with session_scope() as s:
        await staff(s, "Дўстов Жасур", tg=FRIEND)
    await h.say(FRIEND, "Kasalman, xotinim bilan ajrashyapman, uyda muammo bor")
    await h.say(EMP, "Kasalman, ishga kelolmayman")
    mine = ScriptedLLM.calls[-1]
    assert "ajrashyapman" not in mine and "<<<HISTORY" not in mine  # EMP's first message: nothing of the friend
    await h.say(EMP, "boshim ogridi deb aytdimku")
    assert "ajrashyapman" not in ScriptedLLM.calls[-1] and "Kasalman, ishga kelolmayman" in ScriptedLLM.calls[-1]
    # ...and the friend's own memory is still theirs
    await h.say(FRIEND, "ha tayyorlab yubor")
    assert "ajrashyapman" in ScriptedLLM.calls[-1] and "ishga kelolmayman" not in ScriptedLLM.calls[-1]


async def test_unut_erases_only_my_own_memory(h, kb, session_scope, staff):  # noqa: F811
    async with session_scope() as s:
        await staff(s, "Дўстов Жасур", tg=FRIEND)
    await h.say(FRIEND, "Kasalman, uyda muammo bor")
    await h.say(EMP, "Kasalman, ishga kelolmayman")
    replies = await h.say(EMP, "/unut")
    assert "ўчирдим" in replies[0]
    await h.say(EMP, "Kasalman yana")
    assert "<<<HISTORY" not in ScriptedLLM.calls[-1]
    await h.say(FRIEND, "boshim ogridi deb aytdimku")
    assert "uyda muammo bor" in ScriptedLLM.calls[-1]


async def test_old_exchanges_are_forgotten_after_the_set_days(h, kb, session_scope):  # noqa: F811
    from datetime import timedelta

    from hari import memory
    from hari.db.models import ChatTurn
    from hari.worktime import now_utc

    await h.say(EMP, "Kasalman, ishga kelolmayman")
    async with session_scope() as s:
        for turn in (await s.execute(select(ChatTurn))).scalars():
            turn.at = now_utc() - timedelta(days=settings.chat_memory_days + 1)
    await h.say(EMP, "Kasalman bugun ham")
    assert "<<<HISTORY" not in ScriptedLLM.calls[-1]
    async with session_scope() as s:
        assert await memory.purge_old(s, now_utc()) == 1  # the old one; the new exchange stays
        assert len((await s.execute(select(ChatTurn))).scalars().all()) == 1


@pytest.mark.parametrize("text", ["ha", "Ha, tasdiqlayman", "tasdiqlayman", "xo'p", "mayli", "ok", "да",
                                  "ha tayyorlab yubor", "yuboring", "ҳа, юборинг", "да, отправь", "roziman"])
def test_consent_words(text):
    assert is_consent(text)


@pytest.mark.parametrize("text", ["yo'q", "yubormang", "tasdiqlamayman", "нет", "не отправляй", "",
                                  "ha lekin oyligim qachon tushadi", "boshim ogridi deb aytdimku", "оклад"])
def test_not_consent(text):
    assert not is_consent(text)


async def test_the_source_is_named_only_when_asked(h, kb):  # noqa: F811
    first = await h.say(EMP, "Kasalman, ishga kelolmayman")
    assert lat("Тузалиб кетинг") in first[0] and "Manba" not in first[0]  # no source line under the answer
    line = "Манба: HR йўриқномаси — Таътил ва касаллик"
    assert await h.say(EMP, "манбаси?") == [line]
    assert await h.say(EMP, "manbasi qayerda") == [lat(line)]  # asked in Latin: told in Latin
    restarted = Harness()  # a redeploy forgets the chat's memory: the answer log still knows
    assert await restarted.say(EMP, "қаердан билдингиз?") == [line]
