"""Shared set-up for the job-application bot tests: a harness that can also send contacts, photos,
documents and voice messages, and a fixture with the starting vacancies, an owner and an HR account."""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

import pytest
from aiogram.types import Contact, Document, Message, PhotoSize, Update, Voice

from hari.apply import evaluate as E
from hari.apply.vacancies import seed_vacancies
from hari.bot import common
from hari.llm import LLMClient
from tests.test_apply_evaluate import FakeLLM
from tests.test_bot_flow import Harness, btn

APPLICANT, OWNER, HR, WORKER = 601, 602, 603, 604
_ids = itertools.count(10_000)


class ApplyHarness(Harness):
    def _media(self, tg: int, **kw) -> Message:
        return Message(message_id=next(_ids), date=datetime.now(timezone.utc), chat=self._msg(tg).chat,
                       from_user=self._user(tg), **kw)

    async def send(self, tg: int, **kw) -> list[str]:
        start = len(self.session.sent)
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), message=self._media(tg, **kw)))
        return [t for _, t, _ in self.session.sent[start:]]

    async def contact(self, tg: int, phone: str, owner: int | None = None) -> list[str]:
        return await self.send(tg, contact=Contact(phone_number=phone, first_name="x", user_id=owner or tg))

    async def photo(self, tg: int) -> list[str]:
        return await self.send(tg, photo=[PhotoSize(file_id="small", file_unique_id="s", width=10, height=10),
                                          PhotoSize(file_id="big", file_unique_id="b", width=900, height=900)])

    async def document(self, tg: int) -> list[str]:
        return await self.send(tg, document=Document(file_id="cv-file", file_unique_id="cv", file_name="cv.pdf"))

    async def voice(self, tg: int) -> list[str]:
        return await self.send(tg, voice=Voice(file_id="v", file_unique_id="v", duration=3))


@pytest.fixture
async def h_apply(session_scope, staff, monkeypatch):
    common.bot_username = "mgmHaribot"
    monkeypatch.setattr(E, "llm_factory", lambda: LLMClient(api_key="", models=["none"]))  # no AI by default
    async with session_scope() as s:
        await seed_vacancies(s)
        await staff(s, "Раҳбаров Бек", "owner", department="Раҳбарият", position="Директор", tg=OWNER)
        await staff(s, "Кадрова Зайнаб", "hr", department="HR", position="HR масъули", tg=HR)
        await staff(s, "Ишчи Олим", tg=WORKER)
    return ApplyHarness()


def use_ai(monkeypatch, *replies: dict) -> FakeLLM:
    llm = FakeLLM(list(replies))
    monkeypatch.setattr(E, "llm_factory", lambda: llm)
    return llm


async def apply_to_the_end(h: ApplyHarness, tg: int = APPLICANT, *, link: str = "/start apply_garmin",
                           lang: str = "Ўзбекча", consent: str = "Розиман", name: str = "Алиев Вали",
                           vacancy: str = "B2B", skip: str = "Ўтказиб") -> list[str]:
    """The whole application; returns the replies to the last answer."""
    await h.say(tg, link)
    await h.press(tg, btn(h, tg, lang))
    await h.press(tg, btn(h, tg, consent))
    await h.say(tg, name)
    await h.say(tg, "90 123 45 67")
    await h.press(tg, btn(h, tg, vacancy))
    await h.say(tg, "3 йил савдода ишлаганман")
    await h.press(tg, btn(h, tg, skip))
    for _ in range(3):
        await h.press(tg, h.buttons(tg)[0][1])
    await h.say(tg, "Биринчи жавоб")
    return await h.say(tg, "Иккинчи жавоб")


@pytest.fixture
async def h_apply_done(h_apply):
    await apply_to_the_end(h_apply)
    return h_apply
