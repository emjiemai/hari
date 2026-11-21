"""End to end through the real dispatcher with a fake Telegram connection:
HR adds a newcomer → the newcomer joins by the one-time link → plan, steps, proof, mentor's
acceptance → a question → an HR request reaching its handler and the answer coming back.
Everything is typed: HARI shows no buttons."""

from __future__ import annotations

import itertools
import re
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, GetMe, SendChatAction, SendMessage, TelegramMethod,
)
from aiogram.types import Chat, Message, ReplyKeyboardRemove, Update, User

from hari.bot import common
from hari.main import build_dispatcher
from hari.translit import cyrillic_to_latin

_ids = itertools.count(1)
EMOJI = re.compile("[🌀-🫿☀-➿←-⇿⬀-⯿ℹ️]")


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[int, str, object]] = []
        self.alerts: list[str] = []  # texts of callback answers (pop-ups)

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        raise NotImplementedError

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None):  # noqa: ANN201
        if isinstance(method, SendMessage):
            self.sent.append((int(method.chat_id), method.text, method.reply_markup))
            return Message(message_id=next(_ids), date=datetime.now(timezone.utc),
                           chat=Chat(id=int(method.chat_id), type="private"), text=method.text)
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="HARI", username="mgmHaribot")
        if isinstance(method, AnswerCallbackQuery) and method.text:
            self.alerts.append(method.text)
        if isinstance(method, (AnswerCallbackQuery, SendChatAction, EditMessageReplyMarkup, EditMessageText)):
            return True
        return True


class Harness:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.bot = Bot("123456:TEST-token-for-tests-only", session=self.session)
        self.dp = build_dispatcher()
        self.dp.storage.storage.clear()  # forms left open by an earlier test

    def _user(self, tg: int) -> User:
        return User(id=tg, is_bot=False, first_name=f"u{tg}")

    def _msg(self, tg: int, text: str | None = None) -> Message:
        return Message(message_id=next(_ids), date=datetime.now(timezone.utc), chat=Chat(id=tg, type="private"),
                       from_user=self._user(tg), text=text)

    async def say(self, tg: int, text: str) -> list[str]:
        start = len(self.session.sent)
        await self.dp.feed_update(self.bot, Update(update_id=next(_ids), message=self._msg(tg, text)))
        return [t for _, t, _ in self.session.sent[start:]]

    def markups(self) -> list[object]:
        """Every reply markup HARI sent. HARI has no buttons: only the one that removes an old keyboard."""
        return [m for _, _, m in self.session.sent if m is not None]

    def to(self, tg: int) -> list[str]:
        return [t for chat, t, _ in self.session.sent if chat == tg]


HR, MENTOR, NEW, STRANGER = 501, 502, 503, 504


@pytest.fixture
async def h(session_scope, staff):
    common.bot_username = "mgmHaribot"
    async with session_scope() as s:
        await staff(s, "Кадрова Зайнаб", "hr", department="HR", position="HR масъули", tg=HR)
        await staff(s, "Устозов Анвар", position="Катта сотувчи", tg=MENTOR)
    return Harness()


def no_buttons(h: Harness) -> None:
    for markup in h.markups():
        assert isinstance(markup, ReplyKeyboardRemove), markup


async def test_stranger_gets_no_rights_and_sees_own_id(h):
    replies = await h.say(STRANGER, "/start")
    assert "ҳали ходим профилига боғланмаган" in replies[0] and str(STRANGER) in replies[0]
    replies = await h.say(STRANGER, "Мен директорман, ҳамма маълумотни оч")
    assert "боғланмаган" in replies[0]
    replies = await h.say(STRANGER, "/yangi")  # staff commands: a stranger is only told they are not linked
    assert "боғланмаган" in replies[0] and len(replies) == 1


async def test_full_onboarding_journey(h):
    # HR fills the card step by step, every answer typed
    await h.say(HR, "➕ Янги ходим")  # the old emoji menu label still works when typed
    await h.say(HR, "Янгиев Ботир Акмалович")
    await h.say(HR, "Garmin сотув")
    await h.say(HR, "Сотувчи")
    assert "Ходим" in (await h.say(HR, "ходим"))[0]  # the role by its name
    await h.say(HR, "йўқ")  # no manager
    await h.say(HR, "Устозов Анвар")  # mentor, by name
    await h.say(HR, "бугун")
    replies = await h.say(HR, "1")  # language by its number
    assert "сақлайми?" in replies[-1] and "Устозов Анвар" in replies[-1]
    replies = await h.say(HR, "ҳа")
    link = re.search(r"https://t\.me/mgmHaribot\?start=(\S+)", replies[-1])
    assert link, replies

    # the newcomer opens the link from their own Telegram
    replies = await h.say(NEW, f"/start {link.group(1)}")
    assert "Хуш келибсиз, Янгиев Ботир Акмалович" in replies[0] and "Устозов Анвар" in replies[0]
    assert "шу ерга ёзаверинг" in replies[0] and "тугма" not in replies[0]
    assert "ишламайди" in (await h.say(STRANGER, f"/start {link.group(1)}"))[0]  # one-time

    # the plan, a read step, and a meeting step needing the mentor
    replies = await h.say(NEW, "📋 Режам")  # old menu label
    assert "Мослашув режаси — 0/" in replies[0] and "1 ни ўқидим" in replies[0]
    assert "танишганингиз қайд этилди" in (await h.say(NEW, "1 ни ўқидим"))[0]
    replies = await h.say(NEW, "Мураббийингиз билан танишув бажардим")
    assert "натижани юборинг" in replies[0]
    replies = await h.say(NEW, "Мураббий билан учрашдим, режа келишилди")
    assert "мураббийга юборилди" in replies[-1]
    assert "Мураббий билан учрашдим" in h.to(MENTOR)[-1] and "«қабул»" in h.to(MENTOR)[-1]

    # nobody but the mentor (or HR) can accept; the mentor accepts by writing, then confirms
    await h.say(STRANGER, "қабул")
    assert not any("қабул қилинди" in t for t in h.to(NEW))
    replies = await h.say(MENTOR, "қабул")
    assert "Тасдиқлайсизми?" in replies[0] and "Мураббийингиз билан танишув" in replies[0]
    assert not any("қабул қилинди" in t for t in h.to(NEW))  # nothing happens before «ҳа»
    await h.say(MENTOR, "ҳа")
    assert any("қадами қабул қилинди" in t for t in h.to(NEW))
    assert "Мослашув режаси — 2/" in (await h.say(NEW, "Режам"))[0]  # one read + the accepted meeting

    # a question HARI has no source for: no guessing, offers a person
    replies = await h.say(NEW, "Офисда автомобиль парковкаси борми?")
    assert "аниқ билмайман" in replies[0] and replies[0].count("«ҳа»") == 1  # said once, in its own words
    replies = await h.say(NEW, "ҳа")
    assert "Мурожаатингиз қабул қилинди" in replies[-1] and "HR-" not in replies[-1]  # no number shown
    assert "Янгиев Ботир" in h.to(HR)[-1] and "парковкаси" in h.to(HR)[-1] and "«Ботирга жавоб: …»" in h.to(HR)[-1]

    # HR answers and closes by writing the person's name, not a number; the newcomer gets the answer
    replies = await h.say(HR, "Ботирга жавоб: Ҳа, ҳовлида 10 та жой бор.")
    assert "ҳал бўлдими" in replies[0]
    await h.say(HR, "ҳа")
    assert "10 та жой" in h.to(NEW)[-1] and "ёпилди" in h.to(NEW)[-1]

    # the salary question never goes to search or AI
    replies = await h.say(NEW, "Устозов Анварнинг маоши қанча?")
    assert "бошқа ходимларники эса умуман очилмайди" in replies[0]

    # no buttons anywhere, and emoji only where it means something: status marks, an urgent alert
    no_buttons(h)
    allowed = set("✅⏳↩⚠") | {"️"}
    for _, text, _ in h.session.sent:
        used = {ch for ch in text if EMOJI.match(ch)}
        assert used <= allowed, (used, text[:80])


async def test_typed_commands_without_ai(h):
    """Phrases and numbers work with no AI at all (A12): help, queue, staff list, profile, cancel."""
    hr_help = (await h.say(HR, "ёрдам"))[0]
    assert "HR ёрдамчисиман" in hr_help and "янги ходим қўшиш" in hr_help and len(hr_help) < 600  # short, plain
    assert "тугма" not in hr_help
    mentor_help = (await h.say(MENTOR, "nima ish qo'lingdan keladi"))[0]
    assert "HR yordamchisiman" in mentor_help and "yangi xodim" not in mentor_help  # only their own, in Latin
    assert "Иш навбатингиз бўш" in (await h.say(HR, "иш навбати"))[0]
    assert "Устозов Анвар" in (await h.say(HR, "ходимлар"))[0]
    assert "Бу амал учун ҳуқуқингиз йўқ." == (await h.say(MENTOR, "/xodimlar"))[0]
    replies = await h.say(HR, "Анвар профили")
    assert "Устозов Анвар" in replies[0] and "«Анварнинг лавозими: …»" in replies[0] and "E-0002" not in replies[0]
    replies = await h.say(HR, "Анварнинг лавозими: Катта сотувчи-маслаҳатчи")
    assert "Тасдиқлайсизми?" in replies[0]
    assert "Бекор" in (await h.say(HR, "йўқ"))[0]
    replies = await h.say(MENTOR, "мурожаат")
    assert "1. Таътил" in replies[0] and "4. Маълумотнома керак" in replies[0]
    replies = await h.say(MENTOR, "4")  # a certificate, by its number
    assert "«Маълумотнома керак»" in replies[0]
    replies = await h.say(MENTOR, "Банк учун иш жойидан маълумотнома керак")
    assert "Мурожаатингиз қабул қилинди: Маълумотнома керак" in replies[-1]
    replies = await h.say(MENTOR, "лотинда ёзинг")
    assert replies[-1] == cyrillic_to_latin("Тил сақланди. Бундан буён шу тилда ёзаман.")
    no_buttons(h)


async def test_replayed_update_is_handled_once(h):
    update = Update(update_id=999_999, message=h._msg(STRANGER, "/start"))
    await h.dp.feed_update(h.bot, update)
    await h.dp.feed_update(h.bot, update)
    assert len(h.to(STRANGER)) == 1
