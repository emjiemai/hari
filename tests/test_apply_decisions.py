"""Interview / Reject on the report, messages to the applicant, and a lost report sent again by the timer."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select

from hari import scheduler
from hari.apply.applications import start_or_resume, submit
from hari.db.models import Application
from tests.apply_fixtures import APPLICANT, HR, OWNER, WORKER, h_apply, h_apply_done, reject  # noqa: F401
from tests.conftest import FakeBot, at_tashkent
from tests.test_bot_flow import btn

NOW = at_tashkent(date(2026, 11, 9), 10)  # a Monday morning: no Friday report or evening digest in this test


async def test_interview_then_reject_tells_applicant(h_apply_done, session_scope):
    h = h_apply_done
    interview, reject_btn = btn(h, OWNER, "Суҳбатга"), btn(h, HR, "Рад этиш")
    await h.press(OWNER, interview)
    assert "+998901234567" in h.to(APPLICANT)[-1] and "учрашмоқчимиз" in h.to(APPLICANT)[-1]
    asked = await h.press(HR, reject_btn)  # an interview isn't final
    assert "рад этиш асосини ёзинг" in asked[0]
    assert "бошқа номзодни танладик" not in h.to(APPLICANT)[-1]  # nothing is decided until the basis is written
    await h.say(HR, "Суҳбатда мижоз билан ишлаш мисоли бўлмади")
    assert "бошқа номзодни танладик" in h.to(APPLICANT)[-1]
    seen = len(h.to(APPLICANT))
    await h.press(OWNER, interview)  # too late
    assert len(h.to(APPLICANT)) == seen
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    assert app.status == "rejected" and app.decision_basis == "Суҳбатда мижоз билан ишлаш мисоли бўлмади"
    assert "суҳбатга таклиф" not in (await h.say(APPLICANT, "салом"))[0]


async def test_too_late_is_explained_to_the_presser(h_apply_done):
    h = h_apply_done
    await reject(h, HR)
    calls = h.session.alerts
    await h.press(OWNER, btn(h, OWNER, "Суҳбатга"))
    assert any("Кадрова Зайнаб" in a and "Рад этилган" in a for a in calls)


async def test_only_staff_who_hire_can_decide(h_apply_done, session_scope):
    h = h_apply_done
    await h.press(WORKER, btn(h, OWNER, "Рад этиш"))
    await h.press(APPLICANT, btn(h, OWNER, "Рад этиш"))
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    assert app.status == "submitted"
    assert any("ҳуқуқингиз йўқ" in a for a in h.session.alerts)


async def test_show_sends_the_report_again(h_apply_done, session_scope):
    h = h_apply_done
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    from hari.bot.ui import AppActCb
    replies = await h.press(HR, AppActCb(action="show", app_id=app.id).pack())
    assert replies[0].startswith(f"Янги ариза {app.number}")


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
