"""Follow-up replies: HARI reads "yes, send it" and "I told you…" against what was just said, and a "yes"
to the request it offered opens that request instead of asking "which request?" again."""

from __future__ import annotations

import json
import re

import pytest

from hari import answer as A
from hari.guard import is_consent
from hari.knowledge.loader import load_directory
from hari.llm import LLMReply
from tests.conftest import write_doc
from tests.test_bot_flow import HR, MENTOR, Harness  # noqa: F401  (h fixture is defined there)
from tests.test_bot_flow import h  # noqa: F401

EMP = MENTOR  # an ordinary employee with no manager: a leave request goes to HR


class ScriptedLLM:
    """Answers by what the current QUESTION says; records every prompt it got."""

    calls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        pass

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200) -> LLMReply:
        ScriptedLLM.calls.append(user)
        question = re.search(r"<<<QUESTION\n(.*)\nQUESTION>>>", user, re.DOTALL).group(1)
        example = json.loads(re.search(r"<<<EXAMPLES\n(.*)\nEXAMPLES>>>", user, re.DOTALL).group(1))
        ex_id = example[0]["items"][0]["id"]
        if "Kasalman" in question:
            return LLMReply({"answer": "Тузалиб кетинг. Бугунги ишларингизни кимга топширишни раҳбарингиз билан "
                                       "келишинг.", "kind": "grounded", "source_ids": [ex_id], "offer": None}, "fake")
        # a follow-up: only answerable from RECENT CONVERSATION
        assert "<<<HISTORY" in user and "Kasalman" in user, "the earlier turn did not reach the model"
        return LLMReply({"answer": "Раҳбарингизга хабар бериш учун пастдаги тугма орқали мурожаат очинг.",
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
    assert "Тузалиб кетинг" in first[0]

    second = await h.say(EMP, "ha tayyorlab yubor")
    assert "тугма орқали" in second[0] and "Айнан қайси" not in second[0]
    assert any(text.startswith("Мурожаат очиш") for text, _ in h.buttons(EMP))

    calls_before = len(ScriptedLLM.calls)
    third = await h.say(EMP, "tasdiqlayman")
    assert len(ScriptedLLM.calls) == calls_before  # a "yes" to the offer is not another AI question
    assert "Мурожаат қабул қилинди: HR-" in third[-1]
    # HR sees what the person actually said, not only "send it"
    assert "Kasalman" in h.to(HR)[-1] and "ha tayyorlab yubor" in h.to(HR)[-1]


async def test_yes_without_an_offer_goes_to_the_model_with_context(h, kb):  # noqa: F811
    await h.say(EMP, "Kasalman, ishga kelolmayman")
    replies = await h.say(EMP, "ha")  # no button was shown: nothing is opened on a bare "yes"
    assert "Мурожаат қабул қилинди" not in " ".join(replies)
    assert "<<<HISTORY" in ScriptedLLM.calls[-1]


async def test_sensitive_rule_topic_is_not_carried_to_the_model(h, kb):  # noqa: F811
    await h.say(EMP, "Kasalman, ishga kelolmayman")
    await h.say(EMP, "раҳбаримдан шикоят қилмоқчиман")  # fixed reply, never sent to AI
    ScriptedLLM.calls.clear()
    await h.say(EMP, "Kasalman ertaga ham")
    assert "шикоят" not in ScriptedLLM.calls[-1] and "<<<HISTORY" not in ScriptedLLM.calls[-1]


@pytest.mark.parametrize("text", ["ha", "Ha, tasdiqlayman", "tasdiqlayman", "xo'p", "mayli", "ok", "да",
                                  "ha tayyorlab yubor", "yuboring", "ҳа, юборинг", "да, отправь", "roziman"])
def test_consent_words(text):
    assert is_consent(text)


@pytest.mark.parametrize("text", ["yo'q", "yubormang", "tasdiqlamayman", "нет", "не отправляй", "",
                                  "ha lekin oyligim qachon tushadi", "boshim ogridi deb aytdimku", "оклад"])
def test_not_consent(text):
    assert not is_consent(text)
