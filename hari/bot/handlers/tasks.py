"""Work one colleague gives another by writing: "Муслима backend'ни қилсин".

Owners, HR and managers give it: HARI finds the person by name, says once what it will send and waits
for «ҳа», then tells that person and keeps the task. When the person says it is done ("backend'ни
бажардим"), HARI tells whoever gave it. What they say about how it is going ("ярмини қилдим") is kept
and goes to the giver in the evening report (hari.taskwatch, which also asks them how it is going).
"Топшириқлар" shows both sides.
"""

from __future__ import annotations

import re

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy import select

from hari import audit, egress
from hari import intent as I
from hari.agenda import given_name
from hari.bot import texts as T
from hari.bot.context import Ctx, ask_confirm, ask_pick
from hari.bot.ui import notify
from hari.db.models import Employee, Task, TaskUpdate, as_utc
from hari.db.session import session_scope
from hari.translit import cyrillic_to_latin, latin_to_cyrillic, normalize
from hari.worktime import fmt, now_utc

router = Router(name="tasks")
MAX_TEXT = 1000


class Assign(StatesGroup):
    text = State()  # the person is known, what they should do is not yet


# --------------------------------------------------------------------------- who and what

async def _person(ctx: Ctx, args: dict) -> Employee | None:
    """The colleague the task is for: by code from the router, else by a name in the message."""
    async with session_scope() as session:
        if args.get("emp_id"):
            return await session.get(Employee, args["emp_id"])
        code = I.employee_code(str(args.get("who") or ""))
        if code:
            emp = (await session.execute(select(Employee).where(Employee.code == code))).scalar_one_or_none()
            if emp is not None and emp.status != "archived":
                return emp
        everyone = list((await session.execute(select(Employee).where(
            Employee.status != "archived", Employee.id != ctx.emp.id).order_by(Employee.code))).scalars())
    text = normalize(ctx.text.lower())
    fits = [p for p in everyone if any(len(w) >= 4 and normalize(w.lower()) in text for w in p.full_name.split())]
    if len(fits) == 1:
        return fits[0]
    if not fits:
        await ctx.say("Кимга эканини тушунмадим: бу исмли ходимни топмадим.")
        return None
    await ask_pick(ctx, "Кимга?", "assign", {k: v for k, v in args.items() if k != "who"}, "emp_id",
                   [(p.id, f"{p.full_name} — {p.position}") for p in fits[:10]])
    return None


def _without_name(text: str, person: Employee) -> str:
    """The message without the person's name (in either alphabet, with its ending): what is left is the work."""
    for word in person.full_name.split():
        if len(word) < 3:
            continue
        for form in {word, cyrillic_to_latin(word), latin_to_cyrillic(word)}:
            text = re.sub(rf"(?<!\w){re.escape(form)}[\w'ʻʼ‘’]{{0,8}}", " ", text, flags=re.IGNORECASE)
    text = " ".join(text.split()).strip(" ,.:;-—«»\"'")
    return _TELL.sub("", text).strip(" ,.:;-—«»\"'")


_TELL = re.compile(r"^(?:айтинг|айтиб қўйинг|айтиб қўй|айт|ayting|aytib qo'y|ayt|скажите|скажи|передайте|передай|"
                   r"tell)(?!\w)[\s,:;-]*", re.IGNORECASE)


def _clean(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    text = " ".join(text.split()).strip(" «»\"'")
    return text[:MAX_TEXT] if len(text) >= 2 else None


# --------------------------------------------------------------------------- giving it

async def assign(ctx: Ctx, args: dict) -> None:
    person = await _person(ctx, args)
    if person is None:
        return
    if person.id == ctx.emp.id:
        await ctx.say("Ўзингизга топшириқ бера олмайман.")
        return
    if not person.telegram_user_id:
        await ctx.say(f"{person.full_name} ҳали HARI'га уланмаган — хабар унга етмайди.")
        return
    text = _clean(args.get("text")) or (None if args.get("emp_id") else _clean(_without_name(ctx.text, person)))
    if not text:
        await ctx.state.set_state(Assign.text)
        await ctx.state.update_data(emp_id=person.id)
        await ctx.say(f"{given_name(person.full_name)} нима қилиши керак?")
        return
    if not ctx.confirmed:
        await ask_confirm(ctx, f"{person.full_name}га топшириқ юбораман:\n«{text}»", "assign",
                          {"emp_id": person.id, "text": text})
        return
    async with session_scope() as session:
        task = Task(from_id=ctx.emp.id, to_id=person.id, text=text)
        session.add(task)
        await session.flush()
        await audit.record(session, "task.assign", "sent", actor_id=ctx.emp.id, object_type="task",
                           object_id=task.id)
        person = await session.get(Employee, person.id)
    reached = await notify(ctx.bot, person, f"{ctx.emp.full_name}дан топшириқ:\n«{text}»")
    if reached:
        await ctx.say(f"Юборилди: {person.full_name} топшириқни олди. Бажарса, сизга айтаман.")
    else:
        await ctx.say(f"Топшириқ сақланди, лекин {person.full_name}га хабар етмади: у HARI'ни қайта очиши керак.")


@router.message(Assign.text, F.text)
async def text_typed(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.set_state(None)
    await state.update_data(emp_id=None)
    if I.is_no(message.text):
        await message.answer(T.CANCELLED)
        return
    from hari.bot.handlers.chat import ctx_for, run  # here: chat imports this module

    ctx = await ctx_for(message, state, bot)
    if ctx is not None:
        await run(ctx, I.Intent("assign", {"emp_id": data.get("emp_id"), "text": message.text}))


# --------------------------------------------------------------------------- doing it

async def _open_for(session, emp: Employee) -> list[Task]:  # noqa: ANN001
    return list((await session.execute(select(Task).where(Task.to_id == emp.id, Task.status == "open")
                                       .order_by(Task.at))).scalars())


async def _waiting_answer(session, task: Task) -> bool:  # noqa: ANN001
    """HARI asked how it is going and the person has not said anything about it since."""
    if task.asked_at is None:
        return False
    from hari.taskwatch import last_update

    update = await last_update(session, task)
    return update is None or as_utc(update.at) < as_utc(task.asked_at)


async def my_tasks_context(session, emp: Employee) -> list[dict]:  # noqa: ANN001
    """The tasks waiting for this person, for the AI router (refs T1, T2…; the words masked)."""
    out = []
    for n, task in enumerate(await _open_for(session, emp), start=1):
        giver = await session.get(Employee, task.from_id)
        row = {"ref": f"T{n}", "task": egress.outbound(task.text[:120]), "from": giver.code if giver else None}
        if await _waiting_answer(session, task):
            row["hari_asked_how_it_is_going"] = True
        out.append(row)
    return out


async def asked_task(ctx: Ctx) -> I.Intent | None:
    """Without AI: HARI asked about exactly one task and the message is no command → it is the answer."""
    async with session_scope() as session:
        asked = [t for t in await _open_for(session, ctx.emp) if await _waiting_answer(session, t)]
    if len(asked) != 1 or not _clean(ctx.text):
        return None
    return I.Intent("task_status", {"task_id": asked[0].id, "text": ctx.text}, "keyword")


def _chosen(open_: list[Task], args: dict) -> Task | None:
    if args.get("task_id"):
        return next((t for t in open_ if t.id == args["task_id"]), None)
    ref = re.fullmatch(r"[TТ](\d{1,2})", str(args.get("task") or "").strip(), re.IGNORECASE)
    if ref and 1 <= int(ref.group(1)) <= len(open_):
        return open_[int(ref.group(1)) - 1]
    return open_[0] if len(open_) == 1 else None


async def task_status(ctx: Ctx, args: dict) -> None:
    async with session_scope() as session:
        open_ = await _open_for(session, ctx.emp)
        chosen = _chosen(open_, args)
        if chosen is None and not args.get("task_id"):
            asked = [t for t in open_ if await _waiting_answer(session, t)]
            chosen = asked[0] if len(asked) == 1 else None
    if not open_:
        await ctx.say("Сизда очиқ топшириқ йўқ.")
        return
    if chosen is None:
        await ask_pick(ctx, "Қайси топшириқ ҳақида?", "task_status",
                       {k: v for k, v in args.items() if k != "task"}, "task_id",
                       [(t.id, t.text[:80]) for t in open_[:10]])
        return
    note = _clean(args.get("text")) or _clean(ctx.text)
    if not note:
        await ctx.say(f"«{chosen.text}» қандай кетяпти?")
        return
    async with session_scope() as session:
        session.add(TaskUpdate(task_id=chosen.id, text=note))
        await audit.record(session, "task.status", "kept", actor_id=ctx.emp.id, object_type="task",
                           object_id=chosen.id)
        giver = await session.get(Employee, chosen.from_id)
    who = f"{giver.full_name}га" if giver else "Топшириқ берган кишига"
    await ctx.say(f"Ёзиб қўйдим. {who} кечқурун айтаман.")


async def task_done(ctx: Ctx, args: dict) -> None:
    async with session_scope() as session:
        open_ = await _open_for(session, ctx.emp)
    if not open_:
        from hari.bot.handlers import employee  # not a task someone gave: maybe a step of their plan

        await employee.step_done(ctx, {"step": None, "proof": args.get("text")})
        return
    chosen = _chosen(open_, args)
    if chosen is None:
        await ask_pick(ctx, "Қайси топшириқни бажардингиз?", "task_done",
                       {k: v for k, v in args.items() if k != "task"}, "task_id",
                       [(t.id, t.text[:80]) for t in open_[:10]])
        return
    note = _clean(args.get("text"))
    async with session_scope() as session:
        task = await session.get(Task, chosen.id)
        task.status, task.done_at, task.done_text = "done", now_utc(), note
        await audit.record(session, "task.done", "done", actor_id=ctx.emp.id, object_type="task", object_id=task.id)
        giver = await session.get(Employee, task.from_id)
        text = task.text
    await notify(ctx.bot, giver, f"{ctx.emp.full_name} топшириқни бажарди: «{text}»"
                 + (f"\nИзоҳ: {note}" if note else ""))
    await ctx.say(f"Яхши, {giver.full_name if giver else 'топшириқ берган кишига'}га айтдим.")


async def tasks(ctx: Ctx, args: dict) -> None:
    async with session_scope() as session:
        mine = await _open_for(session, ctx.emp)
        given = list((await session.execute(select(Task).where(Task.from_id == ctx.emp.id)
                                            .order_by(Task.at.desc()).limit(15))).scalars())
        names = {e.id: e.full_name for e in (await session.execute(select(Employee).where(
            Employee.id.in_({t.from_id for t in mine} | {t.to_id for t in given})))).scalars()}
    lines = []
    if mine:
        lines.append("Сизга берилган:")
        lines += [f"• «{t.text}» — {names.get(t.from_id, '')}, {fmt(t.at)}" for t in mine]
    if given:
        lines.append(("\n" if lines else "") + "Сиз берганлар:")
        lines += [f"• {names.get(t.to_id, '')}: «{t.text}» — "
                  + (f"бажарилди, {fmt(t.done_at)}" if t.status == "done" else "ҳали бажарилмади") for t in given]
    await ctx.say("\n".join(lines) if lines else "Очиқ топшириқ йўқ.")


ACTIONS = {"assign": assign, "task_done": task_done, "task_status": task_status, "tasks": tasks}
