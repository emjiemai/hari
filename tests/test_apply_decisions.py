"""Interview / Reject on the report, messages to the applicant, and a lost report sent again by the timer."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from hari import scheduler
from hari.apply.applications import start_or_resume, submit
from hari.db.models import Application
from hari.worktime import now_utc
from tests.apply_fixtures import APPLICANT, HR, OWNER, WORKER, h_apply, h_apply_done  # noqa: F401
from tests.conftest import FakeBot
from tests.test_bot_flow import btn


async def test_interview_then_reject_tells_applicant(h_apply_done, session_scope):
    h = h_apply_done
    interview, reject = btn(h, OWNER, "Суҳбатга"), btn(h, HR, "Рад этиш")
    await h.press(OWNER, interview)
    assert "+998901234567" in h.to(APPLICANT)[-1] and "учрашмоқчимиз" in h.to(APPLICANT)[-1]
    await h.press(HR, reject)  # an interview isn't final
    assert "бошқа номзодни танладик" in h.to(APPLICANT)[-1]
    seen = len(h.to(APPLICANT))
    await h.press(OWNER, interview)  # too late
    assert len(h.to(APPLICANT)) == seen
    async with session_scope() as s:
        app = (await s.execute(select(Application))).scalar_one()
    assert app.status == "rejected"
    assert "суҳбатга таклиф" not in (await h.say(APPLICANT, "салом"))[0]


async def test_too_late_is_explained_to_the_presser(h_apply_done):
    h = h_apply_done
    await h.press(HR, btn(h, HR, "Рад этиш"))
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
        await submit(s, app, now_utc() - timedelta(minutes=5))
        fresh, _ = await start_or_resume(s, 9101, "apply")
        fresh.full_name = "Янги Ариза"
        await submit(s, fresh, now_utc())  # under two minutes: the flow itself is still sending it
    bot = FakeBot()
    await scheduler.tick(bot)
    assert [chat for chat, _ in bot.sent] == [1] and "Йўқолган Ариза" in bot.sent[0][1]
    await scheduler.tick(bot)
    assert len(bot.sent) == 1
