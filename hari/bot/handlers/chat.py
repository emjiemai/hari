"""Every typed message no open form is waiting for. HARI has no buttons: this decides what the message
asks and runs it (see hari.intent for how).

Order for a verified employee:
1. «ҳа» to a request HARI just offered → it is opened.
2. A photo or file without words → the result of their task step.
3. A slash command, a whole known phrase or a code typed alone (no AI needed).
4. A topic with a fixed safety reply (pay, passwords, complaints, …) → that reply; it never goes to the AI.
5. The agent (hari.agent) looks up what it needs and plans one or several actions, or answers from data.
   When the AI cannot be asked: the reading of names, numbers and keywords (hari.intent.direct), else the
   message is a question.
Only risky steps (rejecting, deleting, hiring, roles, card changes, everything at once) wait for «ҳа» —
several of them from one message, together, with one «ҳа». Everything else is just done.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from sqlalchemy import select

from hari import agent
from hari import cases as C
from hari import intent as I
from hari import memory
from hari.access import can_manage_employees
from hari.bot import commands
from hari.bot import texts as T
from hari.bot.common import handles_work, staff_by_tg
from hari.bot.context import Ctx, Pending, ask_confirm
from hari.bot.handlers import employee, hiring, people, staff, tasks
from hari.config import settings
from hari.db.models import Employee
from hari.db.session import session_scope
from hari.guard import is_consent, match_rule
from hari.translit import normalize
from hari.worktime import now_utc

router = Router(name="chat")

EXECUTORS = {**employee.ACTIONS, **staff.ACTIONS, **people.ACTIONS, **hiring.ACTIONS, **tasks.ACTIONS}
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
    await commands.refresh(bot, emp, works)  # a new link or a new role: the small menu follows
    file_id = None
    if message.photo:
        file_id = f"photo:{message.photo[-1].file_id}"
    elif message.document:
        file_id = f"doc:{message.document.file_id}"
    return Ctx(message=message, bot=bot, state=state, emp=emp, works=works, manages=can_manage_employees(emp),
               text=(message.text or message.caption or "").strip(), file_id=file_id)


# Steps that wait for «ҳа»: they reject, delete, hire, change roles or cards, or do many things at once.
RISKY = {"app_reject", "app_interview", "app_purge", "app_hold", "app_hire", "employee_edit", "role_change",
         "close_all", "add_person", "new_employee"}
FORMS = {"app_hire", "add_person", "new_employee"}  # they go on with questions of their own


async def run(ctx: Ctx, intent: I.Intent) -> None:
    if not I.allowed(intent.action, ctx.emp.role, ctx.works, ctx.manages):
        await ctx.say(T.NOT_ALLOWED)
        return
    if intent.action not in RISKY:
        ctx.confirmed = True  # a small step: done at once, no «ҳа»
    await EXECUTORS[intent.action](ctx, intent.args)


async def _waiting(ctx: Ctx) -> bool:
    """A question of HARI's waits for this person's answer (a number, «ҳа», a value)."""
    return await ctx.state.get_state() is not None


async def run_plan(ctx: Ctx, plan: agent.Plan) -> None:
    """The agent's plan: its words, then the small steps at once, then the risky ones with one «ҳа»."""
    steps = [s for s in plan.steps if I.allowed(s.action, ctx.emp.role, ctx.works, ctx.manages)]
    if plan.steps and not steps and not plan.say:
        await ctx.say(T.NOT_ALLOWED)
        return
    if plan.say:
        async with session_scope() as session:
            said = await agent.say_text(session, plan.say)
        if said:
            await ctx.say(said)
        elif not steps:
            steps = [I.Intent("question")]
    if ctx.file_id:
        for s in steps:
            if s.action == "step_done":
                s.args["file_id"] = ctx.file_id
    question = next((s for s in steps if s.action == "question"), None)
    steps = [s for s in steps if s.action != "question"]
    for s in [s for s in steps if s.action not in RISKY]:
        await run(ctx, s)
        if await _waiting(ctx):
            return
    risky = [s for s in steps if s.action in RISKY and s.action not in FORMS]
    forms = [s for s in steps if s.action in FORMS]
    if len(risky) == 1:
        await run(ctx, risky[0])
    elif risky:
        ctx.collected = []
        for s in risky:
            await run(ctx, s)
        collected, ctx.collected = ctx.collected, None
        if len(collected) == 1:
            await ask_confirm(ctx, collected[0]["summary"], collected[0]["action"], collected[0]["args"])
        elif collected:
            summary = "\n\n".join(f"{n}. {c['summary']}" for n, c in enumerate(collected, start=1))
            await ask_confirm(ctx, summary, "plan",
                              {"steps": [{"action": c["action"], "args": c["args"]} for c in collected]})
    if forms and not await _waiting(ctx):
        await run(ctx, forms[0])
    if question is not None and not steps and not ctx.said:
        await run(ctx, question)


async def _agent_plan(ctx: Ctx) -> agent.Plan | None:
    if settings.hari_paused:  # the emergency stop: no AI request of any kind
        return None
    names = [n for n in I.ACTIONS if I.allowed(n, ctx.emp.role, ctx.works, ctx.manages)]
    async with session_scope() as session:
        emp = await session.get(Employee, ctx.emp.id)
        context: dict = {"my_steps": employee.steps_context(await employee.my_steps(session, emp)),
                         "my_cases": [{"number": c.number, "type": c.type, "status": c.status}
                                      for c in await C.mine(session, emp) if c.status not in ("closed", "rejected")]}
        mine = await tasks.my_tasks_context(session, emp)
        if mine:
            context["my_tasks"] = mine
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
        me = agent.me_for(emp, ctx.works)
    return await agent.think(me, ctx.text, names, context, [(c, n) for c, n in people_rows], history, now_utc())


async def handle(ctx: Ctx) -> None:
    text = ctx.text
    data = await ctx.state.get_data()
    offer, offered_for = data.get("offer"), data.get("last_question")
    if offer and offered_for and text and (is_consent(text) or await I.decide(
            T.OFFER_QUESTION_HINT if offer == "question" else T.OFFER_CASE_HINT, text) == "yes"):
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

    exact = I.exact(text)
    if exact is not None:
        await run(ctx, exact)
        return
    intent = I.direct(text)
    if intent is not None and (intent.source == "keyword" or intent.action == "close_all") \
            and not I.allowed(intent.action, ctx.emp.role, ctx.works, ctx.manages):
        intent = None  # a likely reading that is no command for this person ("қабул" with no queue)
    if match_rule(text):  # the fixed safety reply; this text never goes to the AI
        await run(ctx, intent if intent is not None and intent.source != "keyword" else I.Intent("question"))
        return
    plan = await _agent_plan(ctx)
    if plan is not None:
        await run_plan(ctx, plan)
        only_question = [s.action for s in plan.steps] == ["question"] and not plan.say
        if not only_question:  # a question's answer is kept by hari.answer itself
            async with session_scope() as session:
                emp = await session.get(Employee, ctx.emp.id)
                await memory.remember(session, emp, text, "\n".join(ctx.said) or "—", now_utc())
        return
    # The AI cannot be asked: read names, numbers and keywords.
    if intent is None or intent.source == "keyword":
        intent = await tasks.asked_task(ctx) or intent  # the answer to "how is it going?"
    intent = intent or I.Intent("question")
    if ctx.file_id and intent.action == "step_done":
        intent.args["file_id"] = ctx.file_id
    await run(ctx, intent)


CONFIRM_HOURS = 12  # a «Тасдиқланг» left unanswered longer than this is dropped, not run later


def _fresh(pending: dict) -> bool:
    try:
        return now_utc() - datetime.fromisoformat(pending["at"]) < timedelta(hours=CONFIRM_HOURS)
    except (KeyError, TypeError, ValueError):
        return True  # asked before the time was kept


@router.message(Pending.confirm, F.text)
async def on_confirm(message: Message, state: FSMContext, bot: Bot) -> None:
    """The answer to «Тасдиқланг»: yes → done; no → not done; anything else → the waiting step is quietly
    dropped and the message is read as a new one. The AI reads replies that are not a plain yes or no."""
    pending = (await state.get_data()).get("pending") or {}
    await state.set_state(None)
    await state.update_data(pending=None)
    ctx = await ctx_for(message, state, bot)
    if ctx is None:
        return
    means = await I.decide(pending.get("summary") or "", message.text) if _fresh(pending) else "other"
    if means == "yes" and pending.get("action") == "plan":
        for step in (pending.get("args") or {}).get("steps") or []:
            ctx.confirmed = True
            await run(ctx, I.Intent(step["action"], dict(step.get("args") or {})))
    elif means == "yes" and pending.get("action"):
        ctx.confirmed = True
        await run(ctx, I.Intent(pending["action"], dict(pending.get("args") or {})))
    elif means == "no":
        await ctx.say("Хўп, қилмадим.")
    else:
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
