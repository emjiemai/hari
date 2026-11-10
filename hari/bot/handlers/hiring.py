"""Owners and HR: decisions on application reports, the vacancy and candidate menus, walk-in applicants.
Every action re-checks the right on the server."""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from hari.access import can_manage_employees
from hari.apply import applications as A
from hari.apply.report import report_buttons, report_parts, send_to, tell_applicant, vacancy_title
from hari.apply.texts import STATUS_LABELS, t
from hari.bot import texts as T
from hari.bot.common import staff_by_tg
from hari.bot.ui import AppActCb
from hari.db.models import Application, Employee
from hari.db.session import session_scope

router = Router(name="hiring")


async def hiring_staff(session, user_id: int) -> Employee | None:  # noqa: ANN001
    emp = await staff_by_tg(session, user_id)
    return emp if emp is not None and can_manage_employees(emp) else None


def too_late(decision: A.Decision) -> str:
    who = decision.decided_by_name or "Бошқа масъул"
    return f"{who} аллақачон қарор қилган: {STATUS_LABELS.get(decision.status, decision.status)}"


@router.callback_query(AppActCb.filter(F.action.in_({"interview", "reject"})))
async def decide(query: CallbackQuery, callback_data: AppActCb, bot: Bot) -> None:
    async with session_scope() as session:
        actor = await hiring_staff(session, query.from_user.id)
        if actor is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        decision = await A.decide(session, callback_data.app_id, actor, callback_data.action)
        app = await session.get(Application, callback_data.app_id)
    if not decision.ok:
        await query.answer(too_late(decision), show_alert=True)
        return
    key = "msg_interview" if callback_data.action == "interview" else "msg_reject"
    delivered = await tell_applicant(bot, app, t(app.language, key, phone=app.phone))
    note = "Белгиланди" + ("" if delivered or not app.telegram_user_id else ". Номзодга хабар етмади")
    await query.answer(T.say(actor, note), show_alert=not delivered and bool(app.telegram_user_id))
    try:
        await query.message.edit_reply_markup(reply_markup=report_buttons(app))
    except Exception:  # noqa: BLE001 — an old message may not be editable; the decision is saved anyway
        pass


@router.callback_query(AppActCb.filter(F.action == "show"))
async def show(query: CallbackQuery, callback_data: AppActCb, bot: Bot) -> None:
    async with session_scope() as session:
        actor = await hiring_staff(session, query.from_user.id)
        app = await session.get(Application, callback_data.app_id) if actor else None
        if actor is None or app is None or app.status == "draft":
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        parts = report_parts(app, await vacancy_title(session, app))
    await query.answer()
    await send_to(bot, actor, parts, report_buttons(app), app.resume_file_id)
