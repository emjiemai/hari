"""Applying feels like talking to a recruiter: the language comes from what they write, a name and phone come
from one sentence, a job is understood from their words, a thin answer gets one follow-up, and every
question is said naturally — while test and work questions stay word for word, nothing reaches the AI before
consent, and their name and phone never reach it at all."""

from __future__ import annotations

from sqlalchemy import select

from hari import voice
from hari.db.models import Application
from hari.llm import LLMReply, LLMUnavailable
from tests.apply_fixtures import APPLICANT, OWNER, h_apply, use_ai, vacancy_number  # noqa: F401


class Voice:
    """The recruiter voice: prefixes a reaction, keeps the rest; records what it saw."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200,
                            temperature: float = 0.1) -> LLMReply:
        self.seen.append(user)
        ask = user.split("ASK:\n<<<\n", 1)[1].rsplit("\n>>>", 1)[0]
        return LLMReply({"text": f"Раҳмат. {ask}" if ask else "Тушунарли."}, "fake")


async def app_row(session_scope) -> Application:
    async with session_scope() as s:
        return (await s.execute(select(Application))).scalar_one()


async def test_the_language_comes_from_what_they_write(h_apply, session_scope):
    h = h_apply
    await h.say(APPLICANT, "/start apply")
    replies = await h.say(APPLICANT, "Здравствуйте, хочу на работу")
    assert "Вы согласны?" in replies[0]
    assert (await app_row(session_scope)).language == "ru"


async def test_name_and_phone_in_one_sentence(h_apply, session_scope):
    h = h_apply
    await h.say(APPLICANT, "/start apply")
    await h.say(APPLICANT, "ўзбекча")
    await h.say(APPLICANT, "ҳа, давом этамиз")
    replies = await h.say(APPLICANT, "Мен Алиев Вали, телефоним 90 123 45 67")
    assert "+998901234567" in replies[0] and "Қайси иш сизни қизиқтиряпти" in replies[-1]  # no phone question
    row = await app_row(session_scope)
    assert row.full_name == "Алиев Вали" and row.phone == "+998901234567" and row.step == "vacancy"


async def test_a_job_named_in_their_own_words(h_apply, monkeypatch, session_scope):
    h = h_apply
    await h.say(APPLICANT, "/start apply")
    await h.say(APPLICANT, "1")
    await h.say(APPLICANT, "ҳа")
    await h.say(APPLICANT, "Алиев Вали 901234567")
    number = int(vacancy_number(h, APPLICANT, "Бухгалтер"))
    use_ai(monkeypatch, {"number": number})  # "ҳисоб-китобни яхши кўраман" → the accountant job
    replies = await h.say(APPLICANT, "ҳисоб-китоб билан ишлашни яхши кўраман")
    assert "Олдин қаерда ишлагансиз" in replies[-1]
    row = await app_row(session_scope)
    assert row.vacancy_id is not None and row.step == "experience"


async def test_a_thin_answer_gets_one_follow_up_shown_in_the_report(h_apply, monkeypatch, session_scope):
    from tests.apply_fixtures import apply_to_the_end  # noqa: F401 — the steps before the work questions

    h = h_apply
    use_ai(monkeypatch, {"questions": ["Мижоз «қиммат» деса, нима дейсиз?", "Биринчи ойда нима қиласиз?"]},
           {"follow_up": "Аниқроқ айтинг: мижозга айнан қандай гап айтардингиз?"},
           {"follow_up": None}, LLMUnavailable("no view"))
    await h.say(APPLICANT, "/start apply_garmin")
    for text in ("1", "ҳа", "Алиев Вали", "90 123 45 67"):
        await h.say(APPLICANT, text)
    await h.say(APPLICANT, vacancy_number(h, APPLICANT, "B2B"))
    for text in ("3 йил савдода", "йўқ", "А", "А", "А"):
        await h.say(APPLICANT, text)
    replies = await h.say(APPLICANT, "Тушунтираман")  # thin
    assert replies[-1] == "Аниқроқ айтинг: мижозга айнан қандай гап айтардингиз?"
    await h.say(APPLICANT, "Сифат ва кафолатни тушунтириб, бўлиб тўлашни таклиф қиламан")
    await h.say(APPLICANT, "Маҳсулотни ўрганаман ва мижозлар базасини тузаман")  # clear: no follow-up
    await h.say(APPLICANT, "Душанбадан, 9–18")
    report = "\n".join(h.to(OWNER))
    assert "Аниқлаштирувчи савол (HARI): Аниқроқ айтинг" in report and "бўлиб тўлашни" in report
    assert "Иш шартлари (бошлаш санаси, кун ва соатлар): Душанбадан, 9–18" in report
    assert (await app_row(session_scope)).status == "submitted"


async def test_the_recruiter_voice_keeps_the_facts_and_never_sees_who_they_are(h_apply, monkeypatch,
                                                                               session_scope):
    h = h_apply
    talk = Voice()
    monkeypatch.setattr(voice, "llm_factory", lambda: talk)
    await h.say(APPLICANT, "/start apply")
    await h.say(APPLICANT, "1")
    assert talk.seen == []  # before consent nothing of theirs goes to the AI
    replies = await h.say(APPLICANT, "ҳа")
    assert replies[-1] == "Раҳмат. Исмингиз ва фамилиянгиз нима?"
    await h.say(APPLICANT, "Алиев Вали")
    await h.say(APPLICANT, "90 123 45 67")
    assert all("Алиев" not in seen and "Вали" not in seen and "123 45 67" not in seen for seen in talk.seen)
    replies = await h.say(APPLICANT, vacancy_number(h, APPLICANT, "B2B"))
    assert replies[-1].startswith("Раҳмат. Олдин қаерда ишлагансиз?")
    for text in ("3 йил савдода", "йўқ"):
        await h.say(APPLICANT, text)
    replies = await h.say(APPLICANT, "А")  # a test question: only a short reaction, the question word for word
    assert replies[-1].startswith("Тушунарли.\n\n2/6.")
