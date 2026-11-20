"""Typed commands instead of buttons (hari.intent): the no-AI reading of phrases and numbers, and the AI
router — it only proposes an action this person may use, never sees names, never invents the text an
action carries, and never gets a topic with a fixed safety reply."""

from __future__ import annotations

from datetime import date

import pytest

from hari import intent as I
from hari import onboarding as O
from hari.config import settings
from hari.llm import LLMReply, LLMUnavailable
from tests.test_bot_flow import HR, MENTOR, Harness, no_buttons

NEW = 506


class Router:
    """A scripted AI router: returns the replies in order and records every prompt."""

    def __init__(self, *replies: dict) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self) -> "Router":
        return self

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200,
                            temperature: float = 0.1) -> LLMReply:
        self.prompts.append(system + "\n" + user)
        if not self.replies:
            raise LLMUnavailable("script ended")
        return LLMReply(self.replies.pop(0), "fake")


@pytest.fixture
async def h(session_scope, staff):
    from hari.bot import common

    common.bot_username = "mgmHaribot"
    async with session_scope() as s:
        await staff(s, "Кадрова Зайнаб", "hr", department="HR", position="HR масъули", tg=HR)
        mentor = await staff(s, "Устозов Анвар", position="Катта сотувчи", tg=MENTOR)
        new = await staff(s, "Янгиев Ботир", tg=NEW, mentor=mentor, start=date(2026, 11, 2))
        await O.create_plan(s, new, date(2026, 11, 2))
    return Harness()


def use_router(monkeypatch, *replies: dict) -> Router:
    router = Router(*replies)
    monkeypatch.setattr(I, "llm_factory", router)
    return router


# --------------------------------------------------------------------------- without AI

@pytest.mark.parametrize("text,action,args", [
    ("📋 Режам", "plan", {}),
    ("rejam", "plan", {}),
    ("Мурожаатларим", "my_cases", {}),
    ("/profil E-0003", "employee_profile", {"who": "E-0003"}),
    ("/rol E-0003 hr", "role_change", {"who": "E-0003", "role": "hr"}),
    ("HR-2026-11-0003 га жавоб: таътил берилди, ёпинг", "case_answer",
     {"number": "HR-2026-11-0003", "text": "таътил берилди, ёпинг", "close": True}),
    ("HR-2026-11-0003 ни олдим", "case_take", {"number": "HR-2026-11-0003"}),
    ("A-2026-11-0001 рад эт: тажриба йўқ", "app_reject", {"number": "A-2026-11-0001", "basis": "тажриба йўқ"}),
    ("A-2026-11-0001 суҳбат баҳоси 3", "app_score", {"number": "A-2026-11-0001", "score": 3}),
    ("A-2026-11-0001 ни суҳбатга чақир", "app_interview", {"number": "A-2026-11-0001"}),
    ("E-0004 лавозими: Катта сотувчи", "employee_edit",
     {"who": "E-0004", "field": "position", "value": "Катта сотувчи"}),
    ("E-0004 ролини раҳбар қил", "role_change", {"who": "E-0004", "role": "manager"}),
    ("123456789 ни ходим қилиб қўш", "add_person", {"tg": 123456789}),
    ("лотинда ёзинг", "set_language", {"lang": "uz_latn"}),
    ("HR-2026-11-0003", "case_show", {"number": "HR-2026-11-0003"}),
])
def test_direct_reading(text, action, args):
    found = I.direct(text)
    assert found is not None and found.action == action and found.args == args and found.source == "direct"


def test_keyword_readings_wait_for_the_ai():
    for text, action in (("қабул", "step_accept"), ("2 ни ўқидим", "step_read"), ("қайтар: расм йўқ", "step_return"),
                         ("3 ни бажардим: мижоз билан гаплашдим", "step_done")):
        found = I.direct(text)
        assert found.action == action and found.source == "keyword", text
    assert I.direct("3 ни бажардим: мижоз билан гаплашдим").args == {"step": "3", "proof": "мижоз билан гаплашдим"}


def test_plain_questions_are_not_commands():
    for text in ("Таътил неча кун?", "Иш вақти қачон тугайди?", "салом"):
        assert I.direct(text) is None, text


def test_yes_no_and_choices():
    assert I.is_yes("ҳа") and I.is_yes("ha") and I.is_yes("да") and I.is_yes("тасдиқлайман")
    assert not I.is_yes("йўқ") and I.is_no("йўқ") and I.is_no("бекор") and I.is_no("нет")
    options = [("manager", ("Бевосита раҳбарим", "раҳбар")), ("owner", ("Компания раҳбари", "эгаси"))]
    assert I.choice("2", options) == "owner" and I.choice("2-си", options) == "owner"
    assert I.choice("компания раҳбари", options) == "owner"  # the longer label wins over "раҳбар"
    assert I.choice("раҳбарим", options) == "manager"
    assert I.choice("9", options) is None and I.choice("бошқа", options) is None


def test_names_become_codes_before_the_ai():
    people = [("E-0002", "Устозов Анвар"), ("E-0003", "Янгиев Ботир")]
    text = I.pseudonymize("Anvarning natijasini qabul qil, Ботирга айт", people)
    assert "Anvar" not in text and "Ботир" not in text and "E-0002" in text and "E-0003" in text


def test_text_arguments_must_be_the_persons_own_words():
    assert I.verbatim("расм  йўқ", "қайтар: Расм йўқ, қайта юборинг") == "расм йўқ"
    assert I.verbatim("Илтимос, расмни қайта юборинг", "қайтар: расм йўқ") is None  # written by the model
    assert I.verbatim(f"{I.egress.MASK} га айт", "x") is None


# --------------------------------------------------------------------------- with the AI router

async def test_the_ai_reads_free_text_as_an_action_and_nothing_happens_before_yes(h, monkeypatch):
    router = use_router(monkeypatch, {"action": "step_done", "args": {"step": "4", "proof": "мураббий билан учрашдик"}})
    replies = await h.say(NEW, "Устозов Анвар aka bilan bugun uchrashdik, мураббий билан учрашдик, режани келишдик")
    assert "Тасдиқлайсизми?" in replies[0] and "«мураббий билан учрашдик»" in replies[0]
    assert not h.to(MENTOR)  # nothing was sent yet
    prompt = router.prompts[0]
    assert "Анвар" not in prompt and "Anvar" not in prompt and "Ботир" not in prompt  # codes, not names
    assert "staff_list" not in prompt and "role_change" not in prompt  # only this person's actions are offered
    await h.say(NEW, "ҳа")
    assert "мураббий билан учрашдик" in h.to(MENTOR)[-1]
    no_buttons(h)


async def test_an_action_the_person_may_not_use_is_never_run(h, monkeypatch):
    use_router(monkeypatch, {"action": "staff_list", "args": {}})
    replies = await h.say(NEW, "ҳамма ходимларни кўрсат")
    assert not any("Ходимлар (✅" in r for r in replies)


async def test_a_reason_the_model_wrote_itself_is_not_used(h, monkeypatch, session_scope):
    use_router(monkeypatch, {"action": "step_done", "args": {"step": "4", "proof": None}})
    await h.say(NEW, "мураббий билан учрашувни қилдим")
    await h.say(NEW, "Учрашдик, режа келишилди")
    use_router(monkeypatch, {"action": "step_return", "args": {"item": "S1", "reason": "Илтимос, батафсилроқ ёзинг"}})
    replies = await h.say(MENTOR, "Ботирнинг натижасини қайтар, кам ёзибди")
    assert "сабабини ёзинг" in replies[0]  # the invented reason was dropped; the mentor writes it
    await h.say(MENTOR, "Кам ёзибди, учрашувда нима келишилганини ёзинг")
    assert "Кам ёзибди" in h.to(NEW)[-1]


async def test_safety_topics_never_reach_the_router(h, monkeypatch):
    router = use_router(monkeypatch)
    replies = await h.say(NEW, "Устозовнинг маоши қанча?")
    assert "бошқа ходимларники эса умуман очилмайди" in replies[0]
    assert router.prompts == []


async def test_no_router_call_while_paused(h, monkeypatch):
    router = use_router(monkeypatch, {"action": "plan", "args": {}})
    monkeypatch.setattr(settings, "hari_paused", True)
    await h.say(NEW, "қадамларимни бир кўрсатинг-чи")
    assert router.prompts == []


async def test_when_the_ai_is_down_keywords_still_work(h, monkeypatch):
    use_router(monkeypatch)  # every call fails
    replies = await h.say(NEW, "1 ни ўқидим")
    assert "танишганингиз қайд этилди" in replies[0]


async def test_which_one_is_asked_with_numbers(h, monkeypatch):
    use_router(monkeypatch, {"action": "step_read", "args": {"step": None}})
    replies = await h.say(NEW, "ўқиб чиқдим")
    assert "Қайси қадам" in replies[0] and "1. Компания миссияси" in replies[0]
    replies = await h.say(NEW, "2")
    assert "«Одоб ва хавфсизлик қоидалари» — танишганингиз қайд этилди." in replies[0]


# --------------------------------------------------------------------------- talking like a person

@pytest.mark.parametrize("text,reply", [
    ("ishlayapsan?", "Ишлаяпман, албатта. Нима керак эди?"),
    ("Ишлаяпсизми", "Ишлаяпман, албатта. Нима керак эди?"),
    ("salom", "Ассалому алайкум! Эшитаман."),
    ("Раҳмат", "Арзимайди!"),
    ("Salom, taʼtil necha kun?", None),  # a question with a hello in front
    ("hisobot qachon", None),  # "hi" only as a whole word
    ("машина ишлаяптими", None),  # about a machine, not about HARI
])
def test_small_talk_without_the_ai(text, reply):
    from hari.answer import small_talk

    assert small_talk(text) == reply


async def test_written_in_latin_is_answered_in_latin(h):
    from hari.translit import cyrillic_to_latin

    replies = await h.say(NEW, "ishlayapsanmi?")  # the AI is not reachable in tests: the small-talk reply
    assert replies == [cyrillic_to_latin("Ишлаяпман, албатта. Нима керак эди?")]
    replies = await h.say(NEW, "Ишлаяпсанми?")  # the same in Cyrillic: Cyrillic back
    assert replies == ["Ишлаяпман, албатта. Нима керак эди?"]


def test_the_prompt_asks_for_a_colleague_not_a_brochure():
    from hari.answer import SYSTEM_PROMPT

    assert "not like a brochure" in SYSTEM_PROMPT and "Ишлаяпман, албатта. Нима керак эди?" in SYSTEM_PROMPT
    assert "say what you can help with" not in SYSTEM_PROMPT
