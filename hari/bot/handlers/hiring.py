"""Owners and HR: decisions on application reports, the interview score, deletion requests, role changes.
Every action re-checks the right on the server.

Spec 2.0: a rejection is recorded only with the basis the deciding person writes (§14); the interview
is scored by that person, never by HARI; a deletion request is decided by a person and a legal hold
stops it (§10, T24); a role change needs a one-time approval bound to that exact change (§25, T19–T20).
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from hari import approvals, owners, retention
from hari.access import can_manage_employees
from hari.apply import applications as A
from hari.apply import rubric
from hari.apply.report import report_buttons, report_parts, send_to, tell_applicant, vacancy_title
from hari.apply.texts import BTN_HOLD, BTN_PURGE, STATUS_LABELS, t
from hari.apply.vacancies import title_for
from hari.bot import texts as T
from hari.bot.common import staff_by_tg
from hari.bot.handlers import staff
from hari.bot.ui import AppActCb, RoleCb, inline, notify
from hari.config import settings
from hari.db.models import Application, Approval, Employee, Vacancy
from hari.db.session import session_scope

router = Router(name="hiring")


class RejectBasis(StatesGroup):
    waiting = State()


async def hiring_staff(session, user_id: int) -> Employee | None:  # noqa: ANN001
    emp = await staff_by_tg(session, user_id)
    return emp if emp is not None and can_manage_employees(emp) else None


def too_late(decision: A.Decision) -> str:
    who = decision.decided_by_name or "Бошқа масъул"
    return f"{who} аллақачон қарор қилган: {STATUS_LABELS.get(decision.status, decision.status)}"


async def _refresh_buttons(query: CallbackQuery, app: Application) -> None:
    try:
        await query.message.edit_reply_markup(reply_markup=report_buttons(app))
    except Exception:  # noqa: BLE001 — an old message may not be editable; the decision is saved anyway
        pass


@router.callback_query(AppActCb.filter(F.action == "interview"))
async def interview(query: CallbackQuery, callback_data: AppActCb, bot: Bot) -> None:
    async with session_scope() as session:
        actor = await hiring_staff(session, query.from_user.id)
        if actor is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        decision = await A.decide(session, callback_data.app_id, actor, "interview")
        app = await session.get(Application, callback_data.app_id)
    if not decision.ok:
        await query.answer(too_late(decision), show_alert=True)
        return
    delivered = await tell_applicant(bot, app, t(app.language, "msg_interview", phone=app.phone))
    note = "Белгиланди" + ("" if delivered or not app.telegram_user_id else ". Номзодга хабар етмади")
    await query.answer(T.say(actor, note), show_alert=not delivered and bool(app.telegram_user_id))
    await _refresh_buttons(query, app)


@router.callback_query(AppActCb.filter(F.action == "reject"))
async def reject_ask(query: CallbackQuery, callback_data: AppActCb, state: FSMContext) -> None:
    """A rejection is a person's decision with a written basis: ask for it before anything is recorded."""
    async with session_scope() as session:
        actor = await hiring_staff(session, query.from_user.id)
        if actor is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        app = await session.get(Application, callback_data.app_id)
        if app is None or app.status not in A.DECISIONS["reject"]:
            who = await session.get(Employee, app.decided_by) if app and app.decided_by else None
            await query.answer(too_late(A.Decision(False, app.status if app else "missing",
                                                   who.full_name if who else None)), show_alert=True)
            return
        number = app.number
    await state.set_state(RejectBasis.waiting)
    await state.update_data(app_id=callback_data.app_id)
    await query.answer()
    await query.message.answer(T.say(actor,
        f"{number}: рад этиш асосини ёзинг — қайси мезон ва қайси далил бўйича. Ёш, жинс, миллат, дин, оилавий "
        "ҳолат ёки мантиқ машқи балли асос бўла олмайди. Ёзувингиз аризада сақланади. Бекор: /bekor"))


@router.message(RejectBasis.waiting, F.text)
async def reject_with_basis(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    app_id = data.get("app_id", "")
    async with session_scope() as session:
        actor = await hiring_staff(session, message.from_user.id)
        if actor is None:
            await message.answer(T.NOT_ALLOWED)
            return
        decision = await A.decide(session, app_id, actor, "reject", basis=message.text)
        app = await session.get(Application, app_id)
    if not decision.ok:
        await message.answer(T.say(actor, too_late(decision)))
        return
    delivered = await tell_applicant(bot, app, t(app.language, "msg_reject"))
    await message.answer(T.say(actor, f"{app.number}: рад этилди, асос сақланди."
                               + ("" if delivered or not app.telegram_user_id else " Номзодга хабар етмади.")))


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


@router.callback_query(AppActCb.filter(F.action == "hire"))
async def hire(query: CallbackQuery, callback_data: AppActCb, state: FSMContext) -> None:
    async with session_scope() as session:
        actor = await hiring_staff(session, query.from_user.id)
        if actor is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        app = await session.get(Application, callback_data.app_id)
        if app is None or not A.can_hire(app):
            who = await session.get(Employee, app.decided_by) if app and app.decided_by else None
            await query.answer(too_late(A.Decision(False, app.status if app else "missing",
                                                   who.full_name if who else None)), show_alert=True)
            return
        vac = await session.get(Vacancy, app.vacancy_id) if app.vacancy_id else None
        position = title_for(vac, "uz_cyrl") if vac else ""
        details = (app.id, app.full_name, vac.department if vac else "", position, app.language)
    await query.answer()
    await staff.start_card_from_application(query.message, state, query.from_user.id, *details)


# --------------------------------------------------------------------------- the interview score (a person's)

@router.callback_query(AppActCb.filter(F.action == "score"))
async def score_ask(query: CallbackQuery, callback_data: AppActCb) -> None:
    async with session_scope() as session:
        actor = await hiring_staff(session, query.from_user.id)
    if actor is None:
        await query.answer(T.NOT_ALLOWED, show_alert=True)
        return
    await query.answer()
    rows = [[(f"{n} — {label}", AppActCb(action=f"iv{n}", app_id=callback_data.app_id))]
            for n, label in rubric.SCALE.items()]
    await query.message.answer(T.say(actor, "Тузилмали суҳбат баҳоси (0–4). Суҳбатни ўтказган киши қўяди; "
                                            "шу роль учун бир хил асосий саволлар бўйича:"), reply_markup=inline(rows))


@router.callback_query(AppActCb.filter(F.action.in_({f"iv{n}" for n in range(5)})))
async def score_set(query: CallbackQuery, callback_data: AppActCb) -> None:
    async with session_scope() as session:
        actor = await hiring_staff(session, query.from_user.id)
        app = await session.get(Application, callback_data.app_id) if actor else None
        if actor is None or app is None or app.purged_at is not None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        app.scorecard = rubric.with_human_score(app.scorecard, "interview", int(callback_data.action[2:]), actor.id)
        lines = rubric.card_lines(app.scorecard)
        number = app.number
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(T.say(actor, f"{number}: суҳбат баҳоси сақланди.\n\n" + "\n".join(lines)))


# --------------------------------------------------------------------------- deletion requests and legal hold

def deletion_buttons(app_id: str):  # noqa: ANN201
    return inline([[(BTN_PURGE, AppActCb(action="purge", app_id=app_id)),
                    (BTN_HOLD, AppActCb(action="hold", app_id=app_id))]])


@router.callback_query(AppActCb.filter(F.action == "purge"))
async def purge(query: CallbackQuery, callback_data: AppActCb, bot: Bot) -> None:
    async with session_scope() as session:
        actor = await hiring_staff(session, query.from_user.id)
        app = await session.get(Application, callback_data.app_id) if actor else None
        if actor is None or app is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        chat, lang, number = app.telegram_user_id, app.language, app.number
        try:
            await retention.delete_now(session, app, actor)
        except retention.RetentionError as exc:
            text = ("Legal hold ўрнатилган: маълумот ўчирилмайди, юрист кўриб чиқади." if str(exc) == "legal_hold"
                    else "Бу ариза маълумоти аллақачон ўчирилган.")
            await query.answer(T.say(actor, text), show_alert=True)
            return
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(T.say(actor, f"{number}: шахсий маълумот ўчирилди. Ариза рақами ва қарор сақланди."))
    if chat:
        try:
            await bot.send_message(chat, t(lang, "delete_done"))
        except Exception:  # noqa: BLE001 — the person may have blocked the bot; the data is deleted either way
            pass


@router.callback_query(AppActCb.filter(F.action == "hold"))
async def hold(query: CallbackQuery, callback_data: AppActCb, bot: Bot) -> None:
    async with session_scope() as session:
        actor = await hiring_staff(session, query.from_user.id)
        app = await session.get(Application, callback_data.app_id) if actor else None
        if actor is None or app is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        await retention.set_legal_hold(session, app, actor, True)
        number, lang = app.number, app.language
        lawyers = (await session.execute(select(Employee).where(
            Employee.role == "lawyer", Employee.status == "active", Employee.telegram_user_id.is_not(None)))).scalars().all()
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(T.say(actor, f"{number}: legal hold ўрнатилди. Автоматик ва қўлда ўчириш тўхтатилди; "
                                            + ("юристга хабар берилди." if lawyers else
                                               "юрист роли тайинланмаган — уни ўзингиз хабардор қилинг.")))
    for lawyer in lawyers:
        await notify(bot, lawyer, f"{number}: номзод маълумотини ўчиришни сўраган, лекин legal hold ўрнатилди "
                                  f"({actor.full_name}). Илтимос, кўриб чиқинг.")
    await tell_applicant(bot, app, t(lang, "delete_hold"))


# --------------------------------------------------------------------------- role change with an approval

@router.message(Command("rol"))
async def role_request(message: Message, command: CommandObject) -> None:
    """/rol E-0003 hr — the owner drafts a role change; it happens only after the one-time confirmation."""
    parts = (command.args or "").split()
    async with session_scope() as session:
        actor = await staff_by_tg(session, message.from_user.id)
        if actor is None or actor.role != "owner":
            await message.answer(T.NOT_ALLOWED)
            return
        if len(parts) != 2:
            await message.answer(T.say(actor, "Мисол: /rol E-0003 hr\nРоллар: " + ", ".join(staff.ROLE_LABELS)))
            return
        target = (await session.execute(select(Employee).where(Employee.code == parts[0].upper()))).scalar_one_or_none()
        if target is None:
            await message.answer(T.say(actor, "Ходим топилмади."))
            return
        try:
            approval = await owners.request_role_change(session, actor, target.id, parts[1].lower())
        except owners.RoleError as exc:
            await message.answer(T.say(actor, str(exc)))
            return
        text = (f"Роль ўзгариши лойиҳаси: {target.code} {target.full_name}\n"
                f"{staff.ROLE_LABELS.get(target.role, target.role)} → {staff.ROLE_LABELS.get(parts[1].lower(), parts[1])}\n\n"
                f"Тасдиқ фақат шу ўзгариш учун, бир марта ва {settings.approval_ttl_minutes} дақиқа амал қилади.")
        buttons = inline([[("Тасдиқлаш", RoleCb(action="ok", emp_id=approval.id, role=parts[1].lower())),
                           ("Бекор", RoleCb(action="no", emp_id=approval.id))]])
    await message.answer(T.say(actor, text), reply_markup=buttons)


@router.callback_query(RoleCb.filter(F.action.in_({"ok", "no"})))
async def role_confirm(query: CallbackQuery, callback_data: RoleCb) -> None:
    if callback_data.action == "no":
        await query.message.edit_reply_markup(reply_markup=None)
        await query.answer(T.CANCELLED)
        return
    try:
        async with session_scope() as session:
            actor = await staff_by_tg(session, query.from_user.id)
            approval = await session.get(Approval, callback_data.emp_id)
            if actor is None or approval is None:
                await query.answer(T.NOT_ALLOWED, show_alert=True)
                return
            try:
                target = await owners.confirm_role_change(session, actor, approval.id, approval.subject_id,
                                                          callback_data.role)
            except approvals.ApprovalError as exc:
                why = {"expired": "тасдиқ муддати ўтди", "payload_changed": "ўзгариш мазмуни бошқа",
                       "version_changed": "ходим ёзуви бу орада ўзгарган", "already_used": "тасдиқ аллақачон ишлатилган",
                       "other_actor": "бу тасдиқ сизники эмас"}.get(exc.reason, exc.reason)
                await query.answer(T.say(actor, f"Бажарилмади: {why}. /rol билан қайта сўранг."), show_alert=True)
                return
            done = f"{target.code} {target.full_name}: роль — {staff.ROLE_LABELS.get(target.role, target.role)}."
    except owners.RoleError as exc:
        await query.answer(str(exc), show_alert=True)
        return
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(T.say(actor, done))
