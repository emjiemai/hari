"""Requests left by test cards are closed once at start-up; real ones are never touched."""

from __future__ import annotations

from sqlalchemy import select

from hari import cases as C
from hari.db.models import HRCase
from hari.housekeeping import close_test_requests, is_placeholder


def test_only_the_forms_example_name_counts():
    assert is_placeholder("Фамилия Исм Отасининг исми") and is_placeholder("familiya ism otasining ismi")
    assert not is_placeholder("Фамилиев Исмоил") and not is_placeholder("Эгаси Шахзод")


async def test_test_requests_are_closed_once_and_real_ones_stay(session_scope, staff):
    async with session_scope() as s:
        tester = await staff(s, "Фамилия Исм Отасининг исми", tg=700)
        real = await staff(s, "Аълоев Анвар", tg=701)
        await C.open_case(s, tester, "other", "HARI жавоб топа олмаган савол")
        await C.open_case(s, tester, "pay_question", "Иш ҳақи ҳисоби бўйича савол")
        await C.open_case(s, real, "certificate", "Банк учун маълумотнома")
    async with session_scope() as s:
        assert await close_test_requests(s) == 2
    async with session_scope() as s:
        assert await close_test_requests(s) == 0  # nothing the second time
        rows = [(c.requester_id, c.status) for c in (await s.execute(select(HRCase))).scalars()]
    assert sorted(st for who, st in rows if who == tester.id) == ["closed", "closed"]
    assert [st for who, st in rows if who == real.id] == ["submitted"]


async def test_an_owner_card_never_shows_a_telegram_number(session_scope, staff):
    from hari.employees import by_telegram
    from hari.housekeeping import unnumber_owner_cards
    from tests.test_bot_flow import Harness

    async with session_scope() as s:
        old = await staff(s, "Эгаси 5519019734", "owner", tg=5519019734)
        other = await staff(s, "Эгаси 111", "owner", tg=111)
        real = await staff(s, "Эгаси Шахзод", "owner", tg=112)
    async with session_scope() as s:
        assert await unnumber_owner_cards(s) == 2
    async with session_scope() as s:
        assert (await by_telegram(s, 111)).full_name == "Эгаси" and (await by_telegram(s, 112)).full_name == "Эгаси Шахзод"
    await Harness().say(5519019734, "салом")  # the next message: the Telegram name (u5519019734 in tests)
    async with session_scope() as s:
        assert "5519019734" not in (await by_telegram(s, 5519019734)).full_name.replace("u5519019734", "")
    assert old and other and real
