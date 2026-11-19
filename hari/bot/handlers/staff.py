"""HR, managers, mentors and other handlers, by writing: new employee cards, the work queue, accepting
onboarding steps and answering HR requests. Every action re-checks the right on the server."""

from __future__ import annotations

from datetime import date, datetime

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy import select

from hari import agenda
from hari import cases as C
from hari import intent as I
from hari import onboarding as O
from hari.access import can_handle_case, can_manage_employees
from hari.apply.applications import mark_hired
from hari.apply.report import tell_applicant
from hari.apply.texts import t as apply_text
from hari.bot import texts as T
from hari.bot.common import invite_link, staff_by_tg
from hari.bot.context import Ctx, ask_confirm, ask_pick
from hari.bot.notifications import case_card
from hari.bot.ui import notify
from hari.config import settings
from hari.db.models import Application, Employee, HRCase, OnboardingPlan, OnboardingStep
from hari.db.session import session_scope
from hari.employees import CardError, CardInput, create_card, list_staff, make_invite
from hari.translit import normalize
from hari.worktime import today_local

router = Router(name="staff")

ROLE_LABELS = {"employee": "Ходим", "manager": "Раҳбар", "hr": "HR", "accountant": "Бухгалтер",
               "lawyer": "Юрист", "it_admin": "IT админ", "owner": "Эгаси"}
LANG_LABELS = {"uz_cyrl": "Ўзбек (кирилл)", "uz_latn": "O'zbek (lotin)", "ru": "Русский"}
ROLE_WORDS = {"employee": ("ходим", "employee", "сотрудник"), "manager": ("раҳбар", "manager", "руководитель"),
              "hr": ("hr", "ҳр", "кадр"), "accountant": ("бухгалтер", "accountant"), "lawyer": ("юрист", "lawyer"),
              "it_admin": ("it админ", "ит админ", "it_admin", "айти"), "owner": ("эгаси", "owner", "владелец")}
LANG_WORDS = {"uz_cyrl": ("кирилл", "ўзбек кирилл", "крилл"), "uz_latn": ("лотин", "lotin", "латин"),
              "ru": ("рус", "русский", "rus")}
NONE_WORDS = {"йўқ", "йук", "кейин", "нет", "—", "-", "0"}


class NewEmp(StatesGroup):
    full_name = State()
    department = State()
    position = State()
    role = State()
    manager = State()
    mentor = State()
    start_date = State()
    language = State()
    confirm = State()


class ReturnReason(StatesGroup):
    waiting = State()


class CaseAnswer(StatesGroup):
    text = State()
    close = State()


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {label}" for i, label in enumerate(items, start=1))


def _cancelled(message: Message) -> bool:
    return bool(message.text) and (I.is_no(message.text) and len(message.text.split()) <= 2)


# --------------------------------------------------------------------------- new employee card

async def _hr(event, session) -> Employee | None:  # noqa: ANN001
    emp = await staff_by_tg(session, event.from_user.id)
    return emp if emp is not None and can_manage_employees(emp) else None


async def new_employee(ctx: Ctx, args: dict) -> None:
    await ctx.state.clear()
    await ctx.state.set_state(NewEmp.full_name)
    await ctx.say("Янги ходим карточкаси. Ҳар қадамда «бекор» деб ёзсангиз, тўхтатаман.\n\n"
                  "1/8. Ходимнинг тўлиқ ФИШини ёзинг (фамилия, исм, отасининг исми):")


@router.message(NewEmp.full_name, F.text)
async def ne_name(message: Message, state: FSMContext) -> None:
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    if len(message.text.split()) < 2:
        await message.answer("Камида фамилия ва исм керак. Қайта ёзинг:")
        return
    await state.update_data(full_name=" ".join(message.text.split()))
    await state.set_state(NewEmp.department)
    await message.answer("2/8. Бўлим (масалан: Garmin сотув, Londry, Бухгалтерия):")


@router.message(NewEmp.department, F.text)
async def ne_dept(message: Message, state: FSMContext) -> None:
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    await state.update_data(department=message.text.strip())
    if (await state.get_data()).get("position"):  # a card from an application already knows the position
        await ask_role(message, state, message.from_user.id)
        return
    await state.set_state(NewEmp.position)
    await message.answer("3/8. Лавозим:")


@router.message(NewEmp.position, F.text)
async def ne_position(message: Message, state: FSMContext) -> None:
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    await state.update_data(position=message.text.strip())
    await ask_role(message, state, message.from_user.id)


def _roles_for(actor: Employee | None) -> list[str]:
    return [r for r in ROLE_LABELS if r != "owner" or (actor is not None and actor.role == "owner")]


async def ask_role(message: Message, state: FSMContext, hr_tg: int, intro: str = "") -> None:
    await state.set_state(NewEmp.role)
    async with session_scope() as session:
        hr = await staff_by_tg(session, hr_tg)
    roles = _roles_for(hr)
    await message.answer(intro + "4/8. HARIдаги роли (ҳуқуқлар шундан келиб чиқади). Рақамини ёки номини ёзинг:\n"
                         + _numbered([ROLE_LABELS[r] for r in roles]))


async def start_card_from_application(message: Message, state: FSMContext, hr_tg: int, app_id: str,
                                      full_name: str, department: str, position: str, language: str) -> None:
    """«Ишга олиш» on a report: the usual card, with what the application already knows filled in."""
    await state.clear()
    await state.update_data(from_application=app_id, full_name=full_name, department=department,
                            position=position, language=language)
    intro = (f"Номзоддан карточка: {full_name}, {position or 'лавозим кўрсатилмаган'}.\n"
             "Ҳар қадамда «бекор» деб ёзсангиз, тўхтатаман.\n\n")
    if not department:
        await state.set_state(NewEmp.department)
        await message.answer(intro + "2/8. Бўлим (масалан: Garmin сотув, Londry, Бухгалтерия):")
    elif not position:
        await state.set_state(NewEmp.position)
        await message.answer(intro + "3/8. Лавозим:")
    else:
        await ask_role(message, state, hr_tg, intro)


async def _people_prompt(state: FSMContext, field: str, title: str) -> str:
    async with session_scope() as session:
        people = (await list_staff(session))[:25]
    await state.update_data(**{f"{field}_options": [p.id for p in people]})
    listing = _numbered([f"{p.full_name} — {p.position} ({p.code})" for p in people])
    return f"{title} Рақамини, исмини ёки кодини ёзинг; йўқ бўлса — «йўқ»:\n{listing}"


async def _person_from_reply(text: str, options: list[str]) -> tuple[bool, str | None]:
    """(understood, employee id or None for "nobody")."""
    words = normalize(text.lower())
    if words in {normalize(w) for w in NONE_WORDS} or text.strip() in NONE_WORDS:
        return True, None
    async with session_scope() as session:
        people = [await session.get(Employee, pid) for pid in options]
        people = [p for p in people if p is not None]
        code = I.employee_code(text)
        if code:
            found = (await session.execute(select(Employee).where(Employee.code == code))).scalar_one_or_none()
            return (True, found.id) if found is not None else (False, None)
    picked = I.choice(text, [(p.id, (p.full_name, *[w for w in p.full_name.split() if len(w) >= 4])) for p in people])
    return (True, picked) if picked else (False, None)


@router.message(NewEmp.role, F.text)
async def ne_role(message: Message, state: FSMContext) -> None:
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    async with session_scope() as session:
        hr = await staff_by_tg(session, message.from_user.id)
    roles = _roles_for(hr)
    role = I.choice(message.text, [(r, (ROLE_LABELS[r], *ROLE_WORDS[r])) for r in roles])
    if role is None:
        await message.answer("Тушунмадим. Ролни рақами билан ёзинг:\n" + _numbered([ROLE_LABELS[r] for r in roles]))
        return
    await state.update_data(role=role)
    await state.set_state(NewEmp.manager)
    await message.answer(f"Роль: {ROLE_LABELS[role]}\n\n" + await _people_prompt(state, "manager", "5/8. Раҳбари."))


@router.message(NewEmp.manager, F.text)
async def ne_manager(message: Message, state: FSMContext) -> None:
    if _cancelled(message) and normalize(message.text.lower()) not in {"йўқ", "йук"}:
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    ok, person = await _person_from_reply(message.text, (await state.get_data()).get("manager_options", []))
    if not ok:
        await message.answer("Бу одамни топмадим. Рўйхатдаги рақамни, кодни ёки «йўқ» деб ёзинг.")
        return
    await state.update_data(manager_id=person)
    await state.set_state(NewEmp.mentor)
    await message.answer(await _people_prompt(state, "mentor", "6/8. Мураббийи."))


@router.message(NewEmp.mentor, F.text)
async def ne_mentor(message: Message, state: FSMContext) -> None:
    if _cancelled(message) and normalize(message.text.lower()) not in {"йўқ", "йук"}:
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    ok, person = await _person_from_reply(message.text, (await state.get_data()).get("mentor_options", []))
    if not ok:
        await message.answer("Бу одамни топмадим. Рўйхатдаги рақамни, кодни ёки «йўқ» деб ёзинг.")
        return
    await state.update_data(mentor_id=person)
    await state.set_state(NewEmp.start_date)
    await message.answer("7/8. Ишни бошлаш санаси (кк.оо.йййй) ёки «бугун»:")


def parse_date(text: str) -> date | None:
    text = text.strip().lower()
    if text in ("бугун", "bugun", "сегодня"):
        return today_local()
    for pattern in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


@router.message(NewEmp.start_date, F.text)
async def ne_start(message: Message, state: FSMContext) -> None:
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    start = parse_date(message.text)
    if start is None:
        await message.answer("Санани кк.оо.йййй шаклида ёзинг, масалан 10.11.2026:")
        return
    await state.update_data(start_date=start.isoformat())
    await state.set_state(NewEmp.language)
    await message.answer("8/8. Мулоқот тили — рақамини ёзинг:\n" + _numbered(list(LANG_LABELS.values())))


@router.message(NewEmp.language, F.text)
async def ne_lang(message: Message, state: FSMContext) -> None:
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    lang = I.choice(message.text, [(code, (label, *LANG_WORDS[code])) for code, label in LANG_LABELS.items()])
    if lang is None:
        await message.answer("Тилни рақами билан ёзинг:\n" + _numbered(list(LANG_LABELS.values())))
        return
    await state.update_data(language=lang)
    await _ne_summary(message, state)


async def _ne_summary(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    async with session_scope() as session:
        mgr = await session.get(Employee, data["manager_id"]) if data.get("manager_id") else None
        mnt = await session.get(Employee, data["mentor_id"]) if data.get("mentor_id") else None
    summary = (
        "Текширинг:\n\n"
        f"ФИШ: {data['full_name']}\nБўлим: {data['department']}\nЛавозим: {data['position']}\n"
        f"Роль: {ROLE_LABELS[data['role']]}\nРаҳбар: {mgr.full_name if mgr else '—'}\n"
        f"Мураббий: {mnt.full_name if mnt else '—'}\n"
        f"Бошлаш: {date.fromisoformat(data['start_date']):%d.%m.%Y}\nТил: {LANG_LABELS[data['language']]}\n\n"
        "Ишга қабул қарори алоҳида тасдиқланган бўлиши керак. Карточкани сақлайми? «ҳа» ёки «йўқ» деб ёзинг."
    )
    await state.set_state(NewEmp.confirm)
    await message.answer(summary)


class _AlreadyDecided(Exception):
    """Someone decided on the application while this card was being filled: undo the card."""


@router.message(NewEmp.confirm, F.text)
async def ne_confirm(message: Message, state: FSMContext, bot: Bot) -> None:
    if not I.is_yes(message.text):
        if I.is_no(message.text):
            await state.clear()
            await message.answer(T.CANCELLED)
        else:
            await message.answer("Сақлаш учун «ҳа», бекор қилиш учун «йўқ» деб ёзинг.")
        return
    data = await state.get_data()
    await state.clear()
    app_id = data.get("from_application")
    app = None
    try:
        async with session_scope() as session:
            hr = await _hr(message, session)
            # Checked again at saving: only HR or an owner, and only an owner may make an owner.
            if hr is None or data.get("role") not in ROLE_LABELS or (data["role"] == "owner" and hr.role != "owner"):
                await message.answer(T.NOT_ALLOWED)
                return
            start = date.fromisoformat(data["start_date"])
            try:
                emp, created = await create_card(session, CardInput(
                    full_name=data["full_name"], department=data["department"], position=data["position"],
                    role=data["role"], manager_id=data.get("manager_id"), mentor_id=data.get("mentor_id"),
                    start_date=start, language=data["language"],
                ), created_by=hr.id)
            except CardError as exc:
                await message.answer(f"Сақланмади: {exc}")
                return
            if app_id:
                if not await mark_hired(session, app_id, hr, emp.id):
                    raise _AlreadyDecided  # leaving the session this way rolls the new card back
                app = await session.get(Application, app_id)
            await O.create_plan(session, emp, start, actor_id=hr.id)
            token = await make_invite(session, emp, created_by=hr.id)
    except _AlreadyDecided:
        await message.answer("Сақланмади: бу номзод бўйича қарор аллақачон қабул қилинган.")
        return
    head = "Карточка сақланди" if created else "Бу ходим аллақачон бор — янги карточка яратилмади"
    link = invite_link(token)
    tail = ("Ҳаволани ходимга шахсан беринг, умумий гуруҳга ташламанг. "
            "У уни ўз Telegram аккаунтидан очиши керак.")
    if app is not None and app.telegram_user_id:
        sent = await tell_applicant(bot, app, apply_text(app.language, "msg_hire", link=link))
        tail = "Ҳавола номзодга юборилди." if sent else "Ҳавола номзодга юборилмади — уни ўзингиз шахсан беринг."
    await message.answer(
        f"{head}: {emp.code} — {emp.full_name}.\nМослашув режаси тайёр.\n\n"
        f"Шахсий ҳавола (бир марталик, {settings.invite_ttl_hours} соат):\n{link}\n\n{tail}"
    )


async def reinvite(ctx: Ctx, args: dict) -> None:
    from hari.bot.handlers.people import find_person

    emp = await find_person(ctx, args, "reinvite", "Кимга янги ҳавола керак?")
    if emp is None:
        return
    async with session_scope() as session:
        hr = await _hr(ctx.message, session)
        if hr is None:
            await ctx.say(T.NOT_ALLOWED)
            return
        token = await make_invite(session, await session.get(Employee, emp.id), created_by=hr.id)
    await ctx.say(f"Янги ҳавола ({emp.code}, {emp.full_name}). Эскиси ишламайди:\n{invite_link(token)}")


async def staff_list(ctx: Ctx, args: dict) -> None:
    async with session_scope() as session:
        people = await list_staff(session)
        plans = {p.employee_id: p for p in (await session.execute(select(OnboardingPlan))).scalars()}
        lines = ["Ходимлар (✅ — Telegram боғланган, ⏳ — ҳали йўқ):"]
        for p in people:
            plan = plans.get(p.id)
            prog = ""
            if plan:
                await session.refresh(plan, ["steps"])
                done, total = O.progress(plan)
                prog = f", мослашув {done}/{total}"
            mark = "✅" if p.telegram_user_id else "⏳"
            lines.append(f"{mark} {p.code} {p.full_name} — {p.position}, {ROLE_LABELS.get(p.role, p.role)}{prog}")
    lines.append("\nПрофил: «E-0003 профили». Ўзгартириш: «E-0003 лавозими: …». Янги ҳавола: «E-0003 га ҳавола».")
    await ctx.say("\n".join(lines)[:4000])


# --------------------------------------------------------------------------- work queue

queue_steps = agenda.queue_steps  # S1, S2…: the same numbers the morning brief uses


async def queue_context(session, me: Employee) -> dict:  # noqa: ANN001
    steps = await queue_steps(session, me)
    cases = await C.open_for_handler(session, me)
    return {"queue_steps": [{"ref": r, "employee": o.code, "step": s.title} for r, s, o in steps],
            "queue_cases": [{"number": c.number, "type": c.type, "status": c.status} for c in cases]}


async def queue(ctx: Ctx, args: dict) -> None:
    async with session_scope() as session:
        me = await session.get(Employee, ctx.emp.id)
        steps = await queue_steps(session, me)
        case_cards = []
        for c in await C.open_for_handler(session, me):
            requester = await session.get(Employee, c.requester_id)
            about = await session.get(Employee, c.about_employee_id) if c.about_employee_id else None
            case_cards.append((c, case_card(c, requester, about)))
    if not steps and not case_cards:
        await ctx.say("Иш навбатингиз бўш.")
        return
    for ref, s, owner in steps:
        await ctx.say(f"{ref}. Мослашув, {owner.full_name}: «{s.title}»\nНатижа: {s.proof_text or '(файл)'}\n\n"
                      f"Қабул: «{ref} ни қабул қил». Қайтариш: «{ref} ни қайтар: сабаб».")
    for c, card in case_cards:
        status = {"submitted": "янги", "accepted": "кўрилмоқда", "decided": "жавоб берилган, очиқ"}.get(c.status, c.status)
        await ctx.say(f"{card}\nҲолат: {status}\n\nЖавоб: «{c.number} га жавоб: …». Олдим: «{c.number} ни олдим».")


# --------------------------------------------------------------------------- accepting onboarding steps

async def _pick_queue_step(ctx: Ctx, args: dict, action: str, question: str) -> tuple[OnboardingStep, Employee] | None:
    async with session_scope() as session:
        me = await session.get(Employee, ctx.emp.id)
        steps = await queue_steps(session, me)
    if args.get("step_id"):
        for _, s, o in steps:
            if s.id == args["step_id"]:
                return s, o
    ref = str(args.get("item") or "").strip().upper()
    if ref and not ref.startswith("S") and ref.isdigit():
        ref = f"S{ref}"
    for r, s, o in steps:
        if r == ref:
            return s, o
    # "Анварнинг натижасини қабул қил": the one person named in the message.
    text = ctx.text.lower()
    named = [(s, o) for _, s, o in steps if o.code.lower() in text
             or any(len(w) >= 4 and normalize(w.lower()) in normalize(text) for w in o.full_name.split())]
    if len(named) == 1:
        return named[0]
    if len(steps) == 1 and not named:
        return steps[0][1], steps[0][2]
    if not steps:
        await ctx.say("Сизни кутаётган мослашув натижаси йўқ.")
        return None
    pool = [(s, o) for s, o in named] or [(s, o) for _, s, o in steps]
    await ask_pick(ctx, question, action, {k: v for k, v in args.items() if k != "item"}, "step_id",
                   [(s.id, f"{o.full_name}: «{s.title}»") for s, o in pool])
    return None


async def step_accept(ctx: Ctx, args: dict) -> None:
    found = await _pick_queue_step(ctx, args, "step_accept", "Қайси натижани қабул қилай?")
    if found is None:
        return
    step, owner = found
    if not ctx.confirmed:
        await ask_confirm(ctx, f"{owner.full_name} — «{step.title}» натижасини қабул қиламан.",
                          "step_accept", {"step_id": step.id})
        return
    async with session_scope() as session:
        me = await session.get(Employee, ctx.emp.id)
        try:
            step, owner = await O.decide(session, me, step.id, accept=True)
        except O.StepError:
            await ctx.say("Қабул қилиб бўлмайди: ҳуқуқ йўқ ёки қадам аллақачон ҳал қилинган.")
            return
        plan = await O.get_plan(session, owner.id)
        finished = plan is not None and plan.status == "done"
        upcoming = None if finished else await agenda.next_step(session, owner)
    await ctx.say(f"Қабул қилинди: {owner.full_name} — «{step.title}».")
    # The newcomer hears it was accepted and, like from a person, what to do next.
    await notify(ctx.bot, owner, T.STEP_ACCEPTED_TO_EMP.format(title=step.title)
                 + (f"\n\n{upcoming}" if upcoming else ""))
    if finished:
        await notify(ctx.bot, owner, T.PLAN_DONE)


async def step_return(ctx: Ctx, args: dict) -> None:
    found = await _pick_queue_step(ctx, args, "step_return", "Қайси натижани қайтарай?")
    if found is None:
        return
    step, owner = found
    reason = args.get("reason")
    if not reason:
        await ctx.state.set_state(ReturnReason.waiting)
        await ctx.state.update_data(step_id=step.id)
        await ctx.say(f"{owner.full_name} — «{step.title}»: қайтариш сабабини ёзинг (нимани тузатиш керак). "
                      "Бекор: «бекор».")
        return
    if not ctx.confirmed:
        await ask_confirm(ctx, f"{owner.full_name} — «{step.title}» натижасини қайтараман. Сабаб: «{reason}».",
                          "step_return", {"step_id": step.id, "reason": reason})
        return
    await _return_step(ctx.message, ctx.bot, ctx.tg, step.id, reason)


async def _return_step(message: Message, bot: Bot, tg: int, step_id: str, reason: str) -> None:
    async with session_scope() as session:
        me = await staff_by_tg(session, tg)
        if me is None:
            return
        try:
            step, owner = await O.decide(session, me, step_id, accept=False, reason=reason)
        except O.StepError:
            await message.answer(T.say(me, "Қайтариб бўлмайди: ҳуқуқ йўқ ёки қадам аллақачон ҳал қилинган."))
            return
    await message.answer(T.say(me, f"Қайтарилди: {owner.full_name} — «{step.title}»."))
    await notify(bot, owner, T.STEP_RETURNED_TO_EMP.format(title=step.title, reason=step.return_reason or "—"))


@router.message(ReturnReason.waiting, F.text)
async def step_return_reason(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    if _cancelled(message):
        await message.answer(T.CANCELLED)
        return
    await _return_step(message, bot, message.from_user.id, data.get("step_id", ""), message.text)


# --------------------------------------------------------------------------- HR requests

async def _pick_case(ctx: Ctx, args: dict, action: str, question: str) -> HRCase | None:
    async with session_scope() as session:
        me = await session.get(Employee, ctx.emp.id)
        mine = await C.open_for_handler(session, me)
        number = args.get("number")
        if number:
            case = await C.get_by_number(session, number)
            if case is None or not can_handle_case(me, case.assignee_id, case.about_employee_id, case.about_role):
                await ctx.say("Бундай мурожаат топилмади ёки уни кўриш ҳуқуқингиз йўқ.")
                return None
            return case
    if len(mine) == 1:
        return mine[0]
    if not mine:
        await ctx.say("Сизда очиқ мурожаат йўқ.")
        return None
    await ask_pick(ctx, question, action, args, "number", [(c.number, f"{c.number} — {c.title}") for c in mine])
    return None


async def case_take(ctx: Ctx, args: dict) -> None:
    case = await _pick_case(ctx, args, "case_take", "Қайси мурожаатни олдингиз?")
    if case is None:
        return
    async with session_scope() as session:
        me = await session.get(Employee, ctx.emp.id)
        case = await C.get_by_number(session, case.number)
        try:
            await C.take(session, me, case)
        except C.CaseError:
            await ctx.say(T.NOT_ALLOWED)
            return
        requester = await session.get(Employee, case.requester_id)
    await ctx.say(f"{case.number}: кўриб чиқаётганингиз қайд этилди. Жавоб: «{case.number} га жавоб: …».")
    await notify(ctx.bot, requester, f"{case.number}: мурожаатингизни {ctx.emp.full_name} кўриб чиқмоқда.")


async def case_answer(ctx: Ctx, args: dict) -> None:
    case = await _pick_case(ctx, args, "case_answer", "Қайси мурожаатга жавоб берасиз?")
    if case is None:
        return
    text, close = args.get("text"), args.get("close")
    if not text:
        await ctx.state.set_state(CaseAnswer.text)
        await ctx.state.update_data(number=case.number)
        await ctx.say(f"{case.number}: ходимга юбориладиган жавоб ёки қарорни ёзинг. Асос ва кейинги қадамни ҳам "
                      "ёзинг. Бекор: «бекор».")
        return
    if close is None:
        await ctx.state.set_state(CaseAnswer.close)
        await ctx.state.update_data(number=case.number, answer=text)
        await ctx.say(_close_question(case.number, text))
        return
    if not ctx.confirmed:
        await ask_confirm(ctx, f"{case.number} га жавоб юбораман: «{text}». "
                               + ("Мурожаат ёпилади." if close else "Мурожаат очиқ қолади."),
                          "case_answer", {"number": case.number, "text": text, "close": bool(close)})
        return
    await _send_answer(ctx.message, ctx.bot, ctx.tg, case.number, text, bool(close))


def _close_question(number: str, text: str) -> str:
    return (f"{number} га жавоб: «{text}»\n\nМасала тўлиқ ҳал бўлдими? «ҳа» — юбориб ёпаман, «очиқ» — юбораман, "
            "мурожаат очиқ қолади, «бекор» — юбормайман.")


@router.message(CaseAnswer.text, F.text)
async def case_answer_text(message: Message, state: FSMContext) -> None:
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    await state.update_data(answer=message.text.strip())
    await state.set_state(CaseAnswer.close)
    number = (await state.get_data())["number"]
    await message.answer(_close_question(number, message.text.strip()))


@router.message(CaseAnswer.close, F.text)
async def case_answer_close(message: Message, state: FSMContext, bot: Bot) -> None:
    words = normalize(message.text.lower())
    if words in ("бекор", "отмена", "cancel") or words.startswith("бекор"):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    if I.is_yes(message.text):
        close = True
    elif words.startswith("очиқ") or words.startswith("йўқ") or words.startswith("нет") or "открыт" in words:
        close = False
    else:
        await message.answer("«ҳа» (ёпаман), «очиқ» (очиқ қолади) ёки «бекор» деб ёзинг.")
        return
    data = await state.get_data()
    await state.clear()
    await _send_answer(message, bot, message.from_user.id, data.get("number", ""), data.get("answer", ""), close)


async def _send_answer(message: Message, bot: Bot, tg: int, number: str, text: str, close: bool) -> None:
    async with session_scope() as session:
        me = await staff_by_tg(session, tg)
        case = await C.get_by_number(session, number)
        if me is None or case is None:
            await message.answer(T.NOT_ALLOWED)
            return
        try:
            await C.answer(session, me, case, text, close)
        except C.CaseError:
            await message.answer(T.NOT_ALLOWED)
            return
        requester = await session.get(Employee, case.requester_id)
    await message.answer(T.say(me, f"Юборилди. {case.number} — {'ёпилди' if close else 'очиқ қолди'}."))
    state_text = "Мурожаат ёпилди." if close else "Мурожаат очиқ: кейинги қадам бажарилгач ёпилади."
    await notify(bot, requester, f"{case.number} — {case.title}\n\nЖавоб ({me.full_name}):\n{case.decision}\n\n"
                                 f"{state_text}\nЭътирозингиз бўлса, шу ерга ёзинг.")


ACTIONS = {
    "new_employee": new_employee, "reinvite": reinvite, "staff_list": staff_list, "queue": queue,
    "step_accept": step_accept, "step_return": step_return, "case_take": case_take, "case_answer": case_answer,
}
