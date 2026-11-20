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
from tests.test_bot_flow import Harness

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
                           lang: str = "1", consent: str = "ҳа", name: str = "Алиев Вали",
                           vacancy: str = "B2B", skip: str = "йўқ") -> list[str]:
    """The whole application, every answer typed; returns the replies to the last answer."""
    await h.say(tg, link)
    await h.say(tg, lang)
    await h.say(tg, consent)
    await h.say(tg, name)
    await h.say(tg, "90 123 45 67")
    await h.say(tg, vacancy_number(h, tg, vacancy))
    await h.say(tg, "3 йил савдода ишлаганман")
    await h.say(tg, skip)
    for _ in range(3):
        await h.say(tg, "А")
    await h.say(tg, "Биринчи жавоб")
    await h.say(tg, "Иккинчи жавоб")
    return await h.say(tg, "Душанбадан, 9 дан 18 гача")  # when they can start, days and hours


def vacancy_number(h: ApplyHarness, tg: int, starts: str) -> str:
    """The number of the listed vacancy whose title starts with `starts`, from the last list HARI sent."""
    for text in reversed(h.to(tg)):
        for line in text.splitlines():
            number, _, title = line.partition(". ")
            if number.isdigit() and title.startswith(starts):
                return number
    raise AssertionError(f"no vacancy {starts!r} listed")


async def reject(h: ApplyHarness, tg: int, basis: str = "Иш намунаси мезони бўйича далил етарли эмас",
                 number: str | None = None) -> list[str]:
    """A rejection is written with its basis (spec 2.0 §14), then confirmed."""
    if number is None:
        from sqlalchemy import select

        from hari.db.models import Application
        from hari.db.session import session_scope

        async with session_scope() as s:
            number = (await s.execute(select(Application.number).where(Application.number.is_not(None))
                                      .order_by(Application.created_at.desc()).limit(1))).scalar_one()
    await h.say(tg, f"{number} рад эт: {basis}")
    return await h.say(tg, "ҳа")


@pytest.fixture
async def h_apply_done(h_apply):
    await apply_to_the_end(h_apply)
    return h_apply
