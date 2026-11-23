"""Every typed message no open form is waiting for. HARI has no buttons: this decides what the message
asks and runs it (see hari.intent for how).

Order for a verified employee:
1. «ҳа» to a request HARI just offered → it is opened.
2. A photo or file without words → the result of their task step.
3. A slash command, a known phrase or a number/code (no AI needed).
4. A topic with a fixed safety reply (pay, passwords, complaints, …) → that reply; it never goes to the AI.
5. The AI router picks an action from those this person may use; when it cannot be asked, the likely
   reading from step 3's keywords, else the message is a question.
Before any change other people will see, the action says what it will do and waits for «ҳа».
"""

from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select

from hari import cases as C
from hari import intent as I
from hari import memory
from hari.access import can_manage_employees
from hari.bot import texts as T
from hari.bot.common import handles_work, staff_by_tg
from hari.bot.context import Ctx, Pending
from hari.bot.handlers import employee, hiring, people, staff
from hari.config import settings
from hari.db.models import Employee
from hari.db.session import session_scope
from hari.guard import is_consent, match_rule
from hari.translit import normalize
from hari.worktime import now_utc

router = Router(name="chat")

EXECUTORS = {**employee.ACTIONS, **staff.ACTIONS, **people.ACTIONS, **hiring.ACTIONS}
assert set(EXECUTORS) == set(I.ACTIONS), set(I.ACTIONS) ^ set(EXECUTORS)

APPLY_WORDS = ("ариза", "ишга кир", "ишга жойлаш", "иш излаяпман", "иш қидир", "вакансия", "работ", "заявк",
               "резюме")


async def ctx_for(message: Message, state: FSMContext, bot: Bot) -> Ctx | None:
    """The verified employee behind this message, or None (no rights at all)."""
    async with session_scope() as session:
        emp = await staff_by_tg(session, message.from_user.id)
        if emp is None:
            return None
        works = await handles_work(session, emp)
    file_id = None
    if message.photo:
        file_id = f"photo:{message.photo[-1].file_id}"
    elif message.document:
        file_id = f"doc:{message.document.file_id}"
    return Ctx(message=message, bot=bot, state=state, emp=emp, works=works, manages=can_manage_employees(emp),
               text=(message.text or message.caption or "").strip(), file_id=file_id)


async def run(ctx: Ctx, intent: I.Intent) -> None:
    if not I.allowed(intent.action, ctx.emp.role, ctx.works, ctx.manages):
        await ctx.say(T.NOT_ALLOWED)
        return
    await EXECUTORS[intent.action](ctx, intent.args)


async def _ai_reading(ctx: Ctx) -> I.Intent | None:
    if settings.hari_paused:  # the emergency stop: no AI request of any kind
        return None
    names = [n for n in I.ACTIONS if I.allowed(n, ctx.emp.role, ctx.works, ctx.manages)]
    async with session_scope() as session:
        emp = await session.get(Employee, ctx.emp.id)
        context: dict = {"my_steps": employee.steps_context(await employee.my_steps(session, emp)),
                         "my_cases": [{"number": c.number, "type": c.type, "status": c.status}
                                      for c in await C.mine(session, emp) if c.status not in ("closed", "rejected")]}
        if ctx.works:
            context |= await staff.queue_context(session, emp)
        if ctx.manages:
            context["applications"] = await hiring.applications_context(session)
        people_rows = list((await session.execute(select(Employee.code, Employee.full_name))).all())
        if ctx.manages:  # "Валини рад эт": an applicant's name becomes their application number for the AI
            from hari.db.models import Application

            people_rows += [(n, f) for n, f in (await session.execute(select(Application.number, Application.full_name)
                            .where(Application.number.is_not(None), Application.full_name != ""))).all() if f]
        history = await memory.recent(session, emp, now_utc())
    return await I.route(ctx.text, names, context, [(c, n) for c, n in people_rows], history)


async def handle(ctx: Ctx) -> None:
    text = ctx.text
    data = await ctx.state.get_data()
    offer, offered_for = data.get("offer"), data.get("last_question")
    if offer and offered_for and text and is_consent(text):
        # "ҳа", "тасдиқлайман", "юбор": the request HARI just offered is opened.
        await employee.take_offer(ctx, offer, offered_for)
        ctype = C.CASE_TYPES.get(offer)
        if ctype is not None and ctype.privacy == "normal":  # a complaint, pay or legal request is not kept
            async with session_scope() as session:
                emp = await session.get(Employee, ctx.emp.id)
                await memory.remember(session, emp, text, f"«{ctype.label}» мурожаати очилди.", now_utc())
        return
    if offer:
        await ctx.state.update_data(offer=None, last_question=None)  # the offer is answered by anything else
    if ctx.file_id and not text:
        await employee.file_sent(ctx)
        return

    intent = I.direct(text)
    if intent is not None and (intent.source == "keyword" or intent.action == "close_all") \
            and not I.allowed(intent.action, ctx.emp.role, ctx.works, ctx.manages):
        intent = None  # a likely reading that is no command for this person ("қабул" with no queue)
    fallback = None
    if intent is not None and intent.source == "keyword":
        fallback, intent = intent, None
    if intent is None and match_rule(text):
        intent = I.Intent("question")  # the fixed safety reply; this text never goes to the router
    if intent is None:
        intent = await _ai_reading(ctx)
    if intent is None:
        intent = fallback or I.Intent("question")
    if ctx.file_id and intent.action == "step_done":
        intent.args["file_id"] = ctx.file_id
    await run(ctx, intent)


@router.message(Pending.confirm, F.text)
async def on_confirm(message: Message, state: FSMContext, bot: Bot) -> None:
    pending = (await state.get_data()).get("pending") or {}
    await state.set_state(None)
    await state.update_data(pending=None)
    ctx = await ctx_for(message, state, bot)
    if ctx is None:
        return
    if I.is_yes(message.text) and pending.get("action"):
        ctx.confirmed = True
        await run(ctx, I.Intent(pending["action"], dict(pending.get("args") or {})))
    elif I.is_no(message.text):
        await ctx.say(T.CANCELLED)
    else:  # something new: the waiting action is not done
        await ctx.say("Олдинги амални бажармадим.")
        await handle(ctx)


@router.message(Pending.pick, F.text)
async def on_pick(message: Message, state: FSMContext, bot: Bot) -> None:
    pending = (await state.get_data()).get("pending") or {}
    await state.set_state(None)
    await state.update_data(pending=None)
    ctx = await ctx_for(message, state, bot)
    if ctx is None:
        return
    if I.is_no(message.text):
        await ctx.say(T.CANCELLED)
        return
    options = [(value, (label,)) for value, label in pending.get("options", [])]
    value = I.choice(message.text, options) if options else None
    if value is None or not pending.get("action"):
        await handle(ctx)
        return
    args = dict(pending.get("args") or {})
    args[pending["key"]] = value
    await run(ctx, I.Intent(pending["action"], args))


@router.message(StateFilter(None), F.text | F.photo | F.document)
async def incoming(message: Message, state: FSMContext, bot: Bot) -> None:
    ctx = await ctx_for(message, state, bot)
    if ctx is not None:
        await handle(ctx)
        return
    # No card: no rights. Someone looking for work can start an application by saying so.
    words = normalize((message.text or "").lower())
    if words and any(normalize(w) in words for w in APPLY_WORDS):
        from hari.bot.handlers.apply import start_application

        await start_application(message, bot)
        return
    await message.answer(T.UNKNOWN_USER.format(tg_id=message.from_user.id))
