"""HARI's own messages reworded by the AI (hari.voice): names, codes and «quotes» never leave the server and
come back exactly; a lost number or marker, a «сен», or no AI at all → the server's own wording."""

from __future__ import annotations

from hari import voice
from hari.config import settings
from hari.llm import LLMReply, LLMUnavailable

BRIEF = ("Хайрли тонг, Анвар. Бугун сиздан кутилаётган ишлар:\n\n"
         "Сизнинг қабулингизни кутаётган натижалар:\n"
         "• S1. Янгиев Ботир — «Мураббийингиз билан танишув». «S1 ни қабул қил» ёки «S1 ни қайтар: …».\n\n"
         "Жавобингизни кутаётган мурожаатлар:\n"
         "• HR-2026-11-0003 — Маълумотнома керак, жавоб муддати 20.11.2026 13:00.")
NAMES = ["Устозов Анвар", "Анвар", "Янгиев Ботир", "Ботир"]


class Fake:
    """Rewords by a function of what it was sent; records what it saw."""

    def __init__(self, rewrite) -> None:  # noqa: ANN001
        self.rewrite, self.seen = rewrite, []

    async def complete_json(self, system: str, user: str, max_tokens: int = 1200,
                            temperature: float = 0.1) -> LLMReply:
        self.seen.append(user)
        if self.rewrite is None:
            raise LLMUnavailable("down")
        return LLMReply({"text": self.rewrite(user.split("<<<\n", 1)[1].rsplit("\n>>>", 1)[0])}, "fake")


def friendly(sent: str) -> str:
    """A rewording that keeps every marker and number, as a good model does."""
    return sent.replace("Хайрли тонг", "Салом").replace("Бугун сиздан кутилаётган ишлар:", "бугун мана булар бор:")


async def test_names_codes_and_quotes_never_reach_the_ai_and_come_back_exactly():
    fake = Fake(friendly)
    out = await voice.natural(BRIEF, NAMES, llm=fake)
    sent = fake.seen[0]
    for secret in ("Анвар", "Ботир", "S1", "HR-2026-11-0003", "Мураббийингиз билан танишув"):
        assert secret not in sent, secret
    assert out.startswith("Салом, Анвар. бугун мана булар бор:")
    for kept in ("S1. Янгиев Ботир", "«S1 ни қабул қил»", "HR-2026-11-0003", "20.11.2026 13:00"):
        assert kept in out, kept


async def test_a_lost_number_or_marker_means_the_servers_wording():
    for broken in (lambda s: s.replace("13:00", "14:00"),  # a deadline changed
                   lambda s: s.replace("⟨1⟩", "", 1),  # a name or command dropped
                   lambda s: s + " ⟨99⟩",  # a marker made up
                   lambda s: "Сен буларни қил: " + s):  # «сен»
        assert await voice.natural(BRIEF, NAMES, llm=Fake(broken)) == BRIEF


async def test_no_ai_or_paused_means_the_servers_wording(monkeypatch):
    assert await voice.natural(BRIEF, NAMES, llm=Fake(None)) == BRIEF
    fake = Fake(friendly)
    monkeypatch.setattr(settings, "hari_paused", True)
    assert await voice.natural(BRIEF, NAMES, llm=fake) == BRIEF and fake.seen == []


def test_a_name_with_an_ending_is_one_marker():
    text, markers = voice.protect("Ботирнинг натижаси", ["Ботир"])
    assert text == "⟨1⟩ натижаси" and markers == {"⟨1⟩": "Ботирнинг"}


async def test_a_reworded_confirmation_through_the_bot(session_scope, staff, monkeypatch):
    from datetime import date

    from hari import onboarding as O
    from tests.test_bot_flow import Harness

    async with session_scope() as s:
        mentor = await staff(s, "Устозов Анвар", tg=902)
        new = await staff(s, "Янгиев Ботир", tg=903, mentor=mentor, start=date(2026, 11, 2))
        await O.create_plan(s, new, date(2026, 11, 2))
        await staff(s, "Эгаси Шахзод", "owner", tg=904)
    fake = Fake(lambda sent: "Хўп, " + sent)
    monkeypatch.setattr(voice, "llm_factory", lambda: fake)
    h = Harness()
    replies = await h.say(904, "E-0002 ролини раҳбар қил")  # a risky step: it asks «ҳа» first
    assert replies[0].startswith("Хўп, ") and "Янгиев Ботир" in replies[0] and "«ҳа»" in replies[0]
    assert all("Ботир" not in seen and "Анвар" not in seen for seen in fake.seen)
    assert "Раҳбар" in (await h.say(904, "ҳа"))[0]
