"""Owners and HR, by writing: decisions on applications, the interview score, deletion requests.
Every action re-checks the right on the server and waits for the person's «ҳа».

Spec 2.0: a rejection is recorded only with the basis the deciding person writes (§14); the interview
is scored by that person, never by HARI; a deletion request is decided by a person and a legal hold
stops it (§10, T24). Role changes (§25, T19–T20) are in hari.bot.handlers.people.
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy import select

from hari import retention
from hari.access import can_manage_employees
from hari.apply import applications as A
from hari.apply import rubric
from hari.apply.report import report_hint, report_parts, send_to, tell_applicant, vacancy_title
from hari.apply.texts import STATUS_LABELS, t
from hari.apply.vacancies import title_for
from hari.bot import texts as T
from hari.bot.common import staff_by_tg
from hari.bot.context import Ctx, ask_confirm, ask_pick
from hari.bot.handlers import staff
from hari.bot.handlers.staff import _cancelled
from hari.bot.ui import notify
from hari.db.models import Application, Employee, Vacancy
from hari.db.session import session_scope

router = Router(name="hiring")


class RejectBasis(StatesGroup):
    waiting = State()


class InterviewScore(StatesGroup):
    waiting = State()


async def hiring_staff(session, user_id: int) -> Employee | None:  # noqa: ANN001
    emp = await staff_by_tg(session, user_id)
    return emp if emp is not None and can_manage_employees(emp) else None


def too_late(decision: A.Decision) -> str:
    who = decision.decided_by_name or "Бошқа масъул"
    return f"{who} аллақачон қарор қилган: {STATUS_LABELS.get(decision.status, decision.status)}"


async def _too_late_for(session, app: Application | None) -> str:  # noqa: ANN001
    who = await session.get(Employee, app.decided_by) if app and app.decided_by else None
    return too_late(A.Decision(False, app.status if app else "missing", who.full_name if who else None))


async def _waiting(session) -> list[Application]:  # noqa: ANN001
    """Applications a person still has to look at: new, at interview, or asking for deletion."""
    rows = (await session.execute(select(Application).where(
        Application.status.in_(("submitted", "interview"))).order_by(Application.submitted_at))).scalars().all()
    asking = (await session.execute(select(Application).where(
        Application.deletion_requested_at.is_not(None), Application.purged_at.is_(None),
        Application.status != "draft"))).scalars().all()
    seen = {a.id for a in rows}
    return list(rows) + [a for a in asking if a.id not in seen]


async def applications_context(session) -> list[dict]:  # noqa: ANN001
    out = []
    for app in (await _waiting(session))[:15]:
        out.append({"number": app.number, "status": app.status, "position": await vacancy_title(session, app),
                    "asks_deletion": bool(app.deletion_requested_at and not app.purged_at)})
    return out


async def _pick_app(ctx: Ctx, args: dict, action: str, question: str) -> Application | None:
    """The application by number; when none is given and exactly one waits, that one; else HARI asks."""
    async with session_scope() as session:
        if await hiring_staff(session, ctx.tg) is None:
            await ctx.say(T.NOT_ALLOWED)
            return None
        if args.get("number"):
            app = (await session.execute(select(Application).where(
                Application.number == args["number"]))).scalar_one_or_none()
            if app is None or app.status == "draft":
                await ctx.say("Бундай ариза топилмади.")
                return None
            return app
        waiting = await _waiting(session)
        labels = [(a.number, f"{a.number} — {a.full_name}, {await vacancy_title(session, a)}") for a in waiting]
    if len(waiting) == 1:
        return waiting[0]
    if not waiting:
        await ctx.say("Ҳозир қарор кутаётган ариза йўқ.")
        return None
    await ask_pick(ctx, question, action, args, "number", labels)
    return None


async def applications(ctx: Ctx, args: dict) -> None:
    async with session_scope() as session:
        if await hiring_staff(session, ctx.tg) is None:
            await ctx.say(T.NOT_ALLOWED)
            return
        waiting = await _waiting(session)
        lines = ["Қарор кутаётган аризалар:"] if waiting else ["Ҳозир қарор кутаётган ариза йўқ."]
        for a in waiting:
            mark = " — маълумотини ўчиришни сўраган" if a.deletion_requested_at and not a.purged_at else ""
            lines.append(f"• {a.number} — {a.full_name}, {await vacancy_title(session, a)}: "
                         f"{STATUS_LABELS.get(a.status, a.status)}{mark}")
    if waiting:
        lines.append("\nҲисобот: «A-… ни кўрсат». Қарор: «A-… ни суҳбатга чақир», «A-… рад эт: асос», «A-… ни ишга ол».")
    await ctx.say("\n".join(lines))


async def app_show(ctx: Ctx, args: dict) -> None:
    app = await _pick_app(ctx, args, "app_show", "Қайси ариза?")
    if app is None:
        return
    async with session_scope() as session:
        actor = await session.get(Employee, ctx.emp.id)
        app = await session.get(Application, app.id)
        parts = report_parts(app, await vacancy_title(session, app))
    await send_to(ctx.bot, actor, parts, report_hint(app), app.resume_file_id)


async def app_interview(ctx: Ctx, args: dict) -> None:
    app = await _pick_app(ctx, args, "app_interview", "Кимни суҳбатга чақирай?")
    if app is None:
        return
    if not ctx.confirmed:
        await ask_confirm(ctx, f"{app.number} — {app.full_name}: суҳбатга чақираман ва номзодга хабар бераман.",
                          "app_interview", {"number": app.number})
        return
    async with session_scope() as session:
        actor = await session.get(Employee, ctx.emp.id)
        decision = await A.decide(session, app.id, actor, "interview")
        app = await session.get(Application, app.id)
    if not decision.ok:
        await ctx.say(too_late(decision))
        return
    delivered = await tell_applicant(ctx.bot, app, t(app.language, "msg_interview", phone=app.phone))
    await ctx.say(f"{app.number}: суҳбатга чақирилди."
                  + ("" if delivered or not app.telegram_user_id else " Номзодга хабар етмади.")
                  + f"\nСуҳбатдан кейин баҳо: «{app.number} суҳбат баҳоси 0–4».")


async def app_reject(ctx: Ctx, args: dict) -> None:
    """A rejection is a person's decision with a written basis: ask for it before anything is recorded."""
    app = await _pick_app(ctx, args, "app_reject", "Кимни рад этасиз?")
    if app is None:
        return
    if app.status not in A.DECISIONS["reject"]:
        async with session_scope() as session:
            await ctx.say(await _too_late_for(session, await session.get(Application, app.id)))
        return
    basis = args.get("basis")
    if not basis:
        await ctx.state.set_state(RejectBasis.waiting)
        await ctx.state.update_data(app_id=app.id)
        await ctx.say(f"{app.number}: рад этиш асосини ёзинг — қайси мезон ва қайси далил бўйича. Ёш, жинс, миллат, "
                      "дин, оилавий ҳолат ёки мантиқ машқи балли асос бўла олмайди. Ёзувингиз аризада сақланади. "
                      "Бекор: «бекор».")
        return
    if not ctx.confirmed:
        await ask_confirm(ctx, f"{app.number} — {app.full_name}: рад этаман. Асос: «{basis}». Номзодга хабар бораман.",
                          "app_reject", {"number": app.number, "basis": basis})
        return
    await _reject(ctx.message, ctx.bot, ctx.tg, app.id, basis)


@router.message(RejectBasis.waiting, F.text)
async def reject_with_basis(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    if _cancelled(message):
        await message.answer(T.CANCELLED)
        return
    await _reject(message, bot, message.from_user.id, data.get("app_id", ""), message.text)


async def _reject(message: Message, bot: Bot, tg: int, app_id: str, basis: str) -> None:
    async with session_scope() as session:
        actor = await hiring_staff(session, tg)
        if actor is None:
            await message.answer(T.NOT_ALLOWED)
            return
        decision = await A.decide(session, app_id, actor, "reject", basis=basis)
        app = await session.get(Application, app_id)
    if not decision.ok:
        await message.answer(T.say(actor, too_late(decision)))
        return
    delivered = await tell_applicant(bot, app, t(app.language, "msg_reject"))
    await message.answer(T.say(actor, f"{app.number}: рад этилди, асос сақланди."
                               + ("" if delivered or not app.telegram_user_id else " Номзодга хабар етмади.")))


async def app_hire(ctx: Ctx, args: dict) -> None:
    app = await _pick_app(ctx, args, "app_hire", "Кимни ишга оласиз?")
    if app is None:
        return
    async with session_scope() as session:
        app = await session.get(Application, app.id)
        if not A.can_hire(app):
            await ctx.say(await _too_late_for(session, app))
            return
        vac = await session.get(Vacancy, app.vacancy_id) if app.vacancy_id else None
        position = title_for(vac, "uz_cyrl") if vac else ""
        details = (app.id, app.full_name, vac.department if vac else "", position, app.language)
    # The card form itself ends with «ҳа»: nothing is saved before that.
    await staff.start_card_from_application(ctx.message, ctx.state, ctx.tg, *details)


# --------------------------------------------------------------------------- the interview score (a person's)

async def app_score(ctx: Ctx, args: dict) -> None:
    app = await _pick_app(ctx, args, "app_score", "Қайси номзоднинг суҳбат баҳоси?")
    if app is None:
        return
    score = args.get("score")
    score = int(score) if isinstance(score, int) or (isinstance(score, str) and score.isdigit()) else None
    if score is None or not 0 <= score <= 4:
        await ctx.state.set_state(InterviewScore.waiting)
        await ctx.state.update_data(app_id=app.id)
        scale = "\n".join(f"{n} — {label}" for n, label in rubric.SCALE.items())
        await ctx.say(f"{app.number}: тузилмали суҳбат баҳоси (0–4). Суҳбатни ўтказган киши қўяди; шу роль учун "
                      f"бир хил асосий саволлар бўйича. Рақамни ёзинг:\n{scale}")
        return
    if not ctx.confirmed:
        await ask_confirm(ctx, f"{app.number} — {app.full_name}: суҳбат баҳоси {score} ({rubric.SCALE[score]}).",
                          "app_score", {"number": app.number, "score": score})
        return
    await _save_score(ctx.message, ctx.tg, app.id, score)


@router.message(InterviewScore.waiting, F.text)
async def score_typed(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    text = message.text.strip()
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    if not (text.isdigit() and 0 <= int(text) <= 4):
        await message.answer("0 дан 4 гача битта рақам ёзинг ёки «бекор».")
        return
    await state.clear()
    await _save_score(message, message.from_user.id, data.get("app_id", ""), int(text))


async def _save_score(message: Message, tg: int, app_id: str, score: int) -> None:
    async with session_scope() as session:
        actor = await hiring_staff(session, tg)
        app = await session.get(Application, app_id) if actor else None
        if actor is None or app is None or app.purged_at is not None:
            await message.answer(T.NOT_ALLOWED)
            return
        app.scorecard = rubric.with_human_score(app.scorecard, "interview", score, actor.id)
        lines = rubric.card_lines(app.scorecard)
        number = app.number
    await message.answer(T.say(actor, f"{number}: суҳбат баҳоси сақланди.\n\n" + "\n".join(lines)))


# --------------------------------------------------------------------------- deletion requests and legal hold

def deletion_hint(number: str) -> str:
    return (f"Қонуний сақлаш асоси бўлмаса, «{number} маълумотини ўчир» деб ёзинг; бўлса — "
            f"«{number} legal hold».")


async def app_purge(ctx: Ctx, args: dict) -> None:
    app = await _pick_app(ctx, args, "app_purge", "Кимнинг маълумотини ўчирай?")
    if app is None:
        return
    if not ctx.confirmed:
        await ask_confirm(ctx, f"{app.number} — {app.full_name}: шахсий маълумотини ўчираман. Ариза рақами ва қарор "
                               "қолади. Буни қайтариб бўлмайди.", "app_purge", {"number": app.number})
        return
    async with session_scope() as session:
        actor = await session.get(Employee, ctx.emp.id)
        app = await session.get(Application, app.id)
        chat, lang, number = app.telegram_user_id, app.language, app.number
        try:
            await retention.delete_now(session, app, actor)
        except retention.RetentionError as exc:
            await ctx.say("Legal hold ўрнатилган: маълумот ўчирилмайди, юрист кўриб чиқади."
                          if str(exc) == "legal_hold" else "Бу ариза маълумоти аллақачон ўчирилган.")
            return
    await ctx.say(f"{number}: шахсий маълумот ўчирилди. Ариза рақами ва қарор сақланди.")
    if chat:
        try:
            await ctx.bot.send_message(chat, t(lang, "delete_done"))
        except Exception:  # noqa: BLE001 — the person may have blocked the bot; the data is deleted either way
            pass


async def app_hold(ctx: Ctx, args: dict) -> None:
    app = await _pick_app(ctx, args, "app_hold", "Қайси аризага legal hold?")
    if app is None:
        return
    if not ctx.confirmed:
        await ask_confirm(ctx, f"{app.number} — {app.full_name}: legal hold ўрнатаман — маълумот автоматик ҳам, "
                               "қўлда ҳам ўчирилмайди, юристга хабар бораман.", "app_hold", {"number": app.number})
        return
    async with session_scope() as session:
        actor = await session.get(Employee, ctx.emp.id)
        app = await session.get(Application, app.id)
        await retention.set_legal_hold(session, app, actor, True)
        number, lang = app.number, app.language
        lawyers = (await session.execute(select(Employee).where(
            Employee.role == "lawyer", Employee.status == "active", Employee.telegram_user_id.is_not(None)))).scalars().all()
    await ctx.say(f"{number}: legal hold ўрнатилди. Автоматик ва қўлда ўчириш тўхтатилди; "
                  + ("юристга хабар берилди." if lawyers else "юрист роли тайинланмаган — уни ўзингиз хабардор қилинг."))
    for lawyer in lawyers:
        await notify(ctx.bot, lawyer, f"{number}: номзод маълумотини ўчиришни сўраган, лекин legal hold ўрнатилди "
                                      f"({actor.full_name}). Илтимос, кўриб чиқинг.")
    await tell_applicant(ctx.bot, app, t(lang, "delete_hold"))


ACTIONS = {
    "applications": applications, "app_show": app_show, "app_interview": app_interview, "app_reject": app_reject,
    "app_hire": app_hire, "app_score": app_score, "app_purge": app_purge, "app_hold": app_hold,
}
