"""Interview / Reject written about a report, messages to the applicant, and a lost report sent again by the timer."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from hari import scheduler
from hari.apply.applications import start_or_resume, submit
from hari.db.models import Application
from tests.apply_fixtures import APPLICANT, HR, OWNER, WORKER, h_apply, h_apply_done, reject  # noqa: F401
from tests.conftest import FakeBot, at_tashkent

NOW = at_tashkent(date(2026, 11, 9), 10)  # a Monday morning: no Friday report or evening digest in this test


async def app_number(session_scope) -> str:
    async with session_scope() as s:
        return (await s.execute(select(Application))).scalar_one().number


async def test_interview_then_reject_tells_applicant(h_apply_done, session_scope):
    h = h_apply_done
    number = await app_number(session_scope)
    assert number not in h.to(OWNER)[-1] and "суҳбатга чақир»" not in h.to(OWNER)[-1]  # by name, no number
    await h.say(OWNER, f"{number} ни суҳбатга чақир")  # a clear order: done at once, no «Тасдиқланг»
    assert "+998901234567" in h.to(APPLICANT)[-1] and "учрашмоқчимиз" in h.to(APPLICANT)[-1]
    asked = await h.say(HR, f"{number} ни рад эт")  # an interview isn't final
    assert "рад этиш асосини ёзинг" in asked[0]
    assert "бошқа номзодни танладик" not in h.to(APPLICANT)[-1]  # nothing is decided until the basis is written
    await h.say(HR, "Суҳбатда мижоз билан ишлаш мисоли бўлмади")
    assert "бошқа номзодни танладик" in h.to(APPLICANT)[-1]
    seen = len(h.to(APPLICANT))
    await h.say(OWNER, f"{number} ни суҳбатга чақир")  # too late
    assert len(h.to(APPLICANT)) == seen
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    assert app.status == "rejected" and app.decision_basis == "Суҳбатда мижоз билан ишлаш мисоли бўлмади"
    assert "суҳбатга таклиф" not in (await h.say(APPLICANT, "салом"))[0]


async def test_too_late_is_explained(h_apply_done, session_scope):
    h = h_apply_done
    number = await app_number(session_scope)
    await reject(h, HR, number=number)
    replies = await h.say(OWNER, f"{number} ни суҳбатга чақир")
    assert "Кадрова Зайнаб" in replies[0] and "Рад этилган" in replies[0]


async def test_only_staff_who_hire_can_decide(h_apply_done, session_scope):
    h = h_apply_done
    number = await app_number(session_scope)
    assert (await h.say(WORKER, f"{number} рад эт: асос"))[0] == "Бу амал учун ҳуқуқингиз йўқ."
    await h.say(APPLICANT, f"{number} рад эт: асос")
    await h.say(APPLICANT, "ҳа")
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    assert app.status == "submitted"


async def test_show_sends_the_report_again(h_apply_done, session_scope):
    h = h_apply_done
    number = await app_number(session_scope)
    replies = await h.say(HR, number)
    assert replies[0].startswith("Янги ариза: Алиев Вали")
    listing = (await h.say(HR, "аризалар"))[0]
    assert "Алиев Вали" in listing and number not in listing


async def test_applications_waiting_are_listed_by_word_or_command(h_apply_done):
    h = h_apply_done
    replies = await h.say(OWNER, "аризалар")
    assert "Алиев Вали" in replies[0]
    replies = await h.say(OWNER, "/arizalar")
    assert "Алиев Вали" in replies[0]


async def test_lost_report_is_resent_by_the_timer(session_scope, staff):
    async with session_scope() as s:
        await staff(s, "Раҳбаров Бек", "owner", tg=1)
        app, _ = await start_or_resume(s, 9100, "apply")
        app.full_name, app.phone = "Йўқолган Ариза", "+998900000000"
        await submit(s, app, NOW - timedelta(minutes=5))
        fresh, _ = await start_or_resume(s, 9101, "apply")
        fresh.full_name = "Янги Ариза"
        await submit(s, fresh, NOW)  # under two minutes: the flow itself is still sending it
    bot = FakeBot()
    await scheduler.tick(bot, NOW)
    assert [chat for chat, _ in bot.sent] == [1] and "Йўқолган Ариза" in bot.sent[0][1]
    await scheduler.tick(bot, NOW)
    assert len(bot.sent) == 1
