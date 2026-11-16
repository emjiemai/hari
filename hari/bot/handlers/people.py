"""Owners see every newcomer and run each card from the bot.

- Someone opens HARI through their invite link: every owner gets the full profile card.
- Someone without a card opens HARI (or a link that no longer works): every owner gets what Telegram
  tells about them, once per account, with a button that makes them an employee (position, name,
  department, role). The owner vouches for who it is; the Telegram ID in the report is the proof.
- /profil E-0003 shows any card. The buttons under it change the position and department (owner or HR)
  and the role (owner only, through the same one-time confirmation as /rol).
"""

from __future__ import annotations

import hashlib

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, User
from loguru import logger
from sqlalchemy import select

from hari import audit, owners
from hari import onboarding as O
from hari.access import can_manage_employees, is_staff
from hari.apply.report import vacancy_title
from hari.apply.texts import STATUS_LABELS as APP_STATUS
from hari.bot import texts as T
from hari.bot.common import handles_work, staff_by_tg
from hari.bot.handlers.staff import ROLE_LABELS
from hari.bot.ui import JoinCb, PersonCb, PickCb, RoleCb, inline, main_menu, notify
from hari.config import settings
from hari.db.models import Application, AuditEvent, Employee
from hari.db.session import session_scope
from hari.employees import CardError, CardInput, create_card
from hari.worktime import fmt, now_utc, today_local

router = Router(name="people")

STATUS = {"candidate": "номзод", "offer": "таклиф берилган", "preparing": "ишга тайёрланмоқда",
          "active": "фаол", "absent": "йўқ (таътил ёки касал)", "exiting": "ишдан кетмоқда", "archived": "архивда"}
LANGS = {"uz_cyrl": "ўзбек (кирилл)", "uz_latn": "ўзбек (лотин)", "ru": "рус"}
DAYS = ["Ду", "Се", "Чо", "Па", "Жу", "Ша", "Як"]
FIELDS = {"pos": ("position", "лавозим"), "dept": ("department", "бўлим")}


class AddPerson(StatesGroup):
    position = State()
    full_name = State()
    department = State()
    role = State()
    confirm = State()


class EditPerson(StatesGroup):
    value = State()


async def _manager(event, session) -> Employee | None:  # noqa: ANN001
    emp = await staff_by_tg(session, event.from_user.id)
    return emp if emp is not None and can_manage_employees(emp) else None


async def owners_to_tell(session, exclude_id: str | None = None) -> list[Employee]:  # noqa: ANN001
    rows = (await session.execute(select(Employee).where(
        Employee.role == "owner", Employee.status == "active", Employee.telegram_user_id.is_not(None),
    ))).scalars().all()
    return [o for o in rows if o.id != exclude_id and is_staff(o)]


# --------------------------------------------------------------------------- the profile card

async def profile_card(session, emp: Employee) -> str:  # noqa: ANN001
    """Everything HARI holds on the card, for an owner or HR. Never chat text or request contents."""
    manager = await session.get(Employee, emp.manager_id) if emp.manager_id else None
    mentor = await session.get(Employee, emp.mentor_id) if emp.mentor_id else None
    plan = await O.get_plan(session, emp.id)
    onboarding = "—"
    if plan is not None:
        await session.refresh(plan, ["steps"])
        done, total = O.progress(plan)
        onboarding = f"{done}/{total} қадам"
    days = ", ".join(DAYS[int(d)] for d in emp.work_days.split(",") if d.strip().isdigit() and int(d) < 7)
    tg = (f"{emp.telegram_user_id} (боғланган {fmt(emp.identity_verified_at)})" if emp.telegram_user_id
          else "ҳали боғланмаган")
    lines = [
        emp.full_name,
        f"ID: {emp.code}",
        f"Telegram ID: {tg}",
        f"Ҳолат: {STATUS.get(emp.status, emp.status)}",
        f"Бўлим: {emp.department}",
        f"Лавозим: {emp.position}",
        f"Роль: {ROLE_LABELS.get(emp.role, emp.role)}",
        f"Раҳбар: {manager.full_name if manager else '—'}",
        f"Мураббий: {mentor.full_name if mentor else '—'}",
        f"Бошлаш санаси: {emp.start_date:%d.%m.%Y}" if emp.start_date else "Бошлаш санаси: —",
        f"Иш вақти: {emp.work_start_hour}:00–{emp.work_end_hour}:00, {days or '—'}",
        f"Тил: {LANGS.get(emp.language, emp.language)}",
        f"Мослашув: {onboarding}",
        f"Карточка очилган: {fmt(emp.created_at)}",
    ]
    app = (await session.execute(select(Application).where(Application.employee_id == emp.id)
                                 .order_by(Application.created_at.desc()).limit(1))).scalar_one_or_none()
    if app is not None:
        lines.append(f"Ариза: {await vacancy_title(session, app) or '—'}, {fmt(app.submitted_at or app.created_at)}")
    return "\n".join(lines)


def card_buttons(viewer: Employee, emp: Employee):  # noqa: ANN201
    rows = [[("Лавозимни белгилаш", PersonCb(action="pos", emp_id=emp.id))],
            [("Бўлимни ўзгартириш", PersonCb(action="dept", emp_id=emp.id))]]
    if viewer.role == "owner":
        rows.append([("Ролни ўзгартириш", PersonCb(action="role", emp_id=emp.id))])
    return inline([[(T.say(viewer, text), cb) for text, cb in row] for row in rows])


async def report_join(bot: Bot, emp_id: str) -> None:
    """Someone opened their invite link: every owner sees the whole card, with the buttons to fix it."""
    async with session_scope() as session:
        emp = await session.get(Employee, emp_id)
        if emp is None:
            return
        card = await profile_card(session, emp)
        targets = [(o, card_buttons(o, emp)) for o in await owners_to_tell(session, exclude_id=emp.id)]
    for owner, buttons in targets:
        await notify(bot, owner, f"Янги ходим HARI'га қўшилди.\n\n{card}", buttons)


def _seen_key(telegram_user_id: int) -> str:
    # The audit row marks "already reported" without writing the raw Telegram ID to the log.
    return hashlib.sha256(f"stranger:{telegram_user_id}".encode()).hexdigest()[:40]


async def report_stranger(bot: Bot, user: User, why: str = "") -> None:
    """Someone with no card opened HARI. Owners hear about each account once."""
    key = _seen_key(user.id)
    async with session_scope() as session:
        if await staff_by_tg(session, user.id) is not None:
            return
        seen = (await session.execute(select(AuditEvent.id).where(
            AuditEvent.action == "stranger.report", AuditEvent.object_id == key).limit(1))).first()
        if seen is not None:
            return
        targets = await owners_to_tell(session)
        if not targets:
            return
        await audit.record(session, "stranger.report", "sent", object_type="telegram", object_id=key)
        app = (await session.execute(select(Application).where(Application.telegram_user_id == user.id)
                                     .order_by(Application.created_at.desc()).limit(1))).scalar_one_or_none()
        applied = "йўқ"
        if app is not None:
            applied = (f"{await vacancy_title(session, app) or 'лавозим кўрсатилмаган'} — "
                       f"{APP_STATUS.get(app.status, app.status)}, {fmt(app.submitted_at or app.created_at)}")
    name = " ".join(f"{user.first_name or ''} {user.last_name or ''}".split()) or "—"
    text = "\n".join([
        f"HARI'ни профили йўқ одам очди{why}.",
        "",
        f"Telegram'даги исми: {name}",
        f"Username: @{user.username}" if user.username else "Username: —",
        f"Telegram ID: {user.id}",
        f"Telegram тили: {user.language_code or '—'}",
        f"Вақт: {fmt(now_utc())}",
        f"Ишга ариза: {applied}",
        "",
        "Ходимингиз бўлса, пастдаги тугма билан қўшинг: лавозими, исми, бўлими ва роли сўралади. "
        "Танимасангиз, ҳеч нарса қилманг — унинг ҳеч қандай ҳуқуқи йўқ.",
    ])
    lang = "ru" if (user.language_code or "").startswith("ru") else "uz_cyrl"
    for owner in targets:
        await notify(bot, owner, text, inline([[(T.say(owner, "Ходим қилиб қўшиш"),
                                                 JoinCb(action="add", tg=user.id, lang=lang))]]))


# --------------------------------------------------------------------------- /profil

@router.message(Command("profil", "profile"))
async def show_profile(message: Message, command: CommandObject) -> None:
    arg = (command.args or "").strip()
    async with session_scope() as session:
        actor = await _manager(message, session)
        if actor is None:
            await message.answer(T.NOT_ALLOWED)
            return
        if not arg:
            await message.answer(T.say(actor, "Мисол: /profil E-0003 (ёки Telegram ID). Рўйхат: /xodimlar"))
            return
        where = (Employee.telegram_user_id == int(arg)) if arg.isdigit() else (Employee.code == arg.upper())
        emp = (await session.execute(select(Employee).where(where))).scalar_one_or_none()
        if emp is None:
            await message.answer(T.say(actor, "Ходим топилмади."))
            return
        card, buttons = await profile_card(session, emp), card_buttons(actor, emp)
    await message.answer(T.say(actor, card), reply_markup=buttons)


# --------------------------------------------------------------------------- position and department

@router.callback_query(PersonCb.filter(F.action.in_(set(FIELDS))))
async def edit_field(query: CallbackQuery, callback_data: PersonCb, state: FSMContext) -> None:
    async with session_scope() as session:
        actor = await _manager(query, session)
        emp = await session.get(Employee, callback_data.emp_id)
        if actor is None or emp is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        attr, label = FIELDS[callback_data.action]
        current = getattr(emp, attr)
    await state.clear()
    await state.set_state(EditPerson.value)
    await state.update_data(emp_id=emp.id, field=callback_data.action)
    await query.answer()
    await query.message.answer(T.say(actor, f"{emp.full_name}: ҳозирги {label} — «{current}».\n"
                                            f"Янги {label}ни ёзинг (/bekor — бекор қилиш):"))


@router.message(EditPerson.value, F.text)
async def save_field(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    value = " ".join(message.text.split())
    if not value or len(value) > 100 or value.startswith("/"):
        await message.answer("1–100 белги бўлсин. Қайта ёзинг ёки /bekor:")
        return
    await state.clear()
    attr, label = FIELDS.get(data.get("field"), (None, None))
    async with session_scope() as session:
        actor = await _manager(message, session)
        emp = await session.get(Employee, data.get("emp_id", ""))
        if actor is None or emp is None or attr is None:
            await message.answer(T.NOT_ALLOWED)
            return
        old = getattr(emp, attr)
        setattr(emp, attr, value)
        await audit.record(session, "employee.edit", "changed", actor_id=actor.id, object_type="employee",
                           object_id=emp.id, detail=attr)
        await session.flush()
        card, buttons = await profile_card(session, emp), card_buttons(actor, emp)
        person = emp if emp.id != actor.id else None
    await message.answer(T.say(actor, f"Сақланди: {label} «{old}» → «{value}».\n\n{card}"), reply_markup=buttons)
    await notify(bot, person, f"Профилингиз янгиланди: {label} — {value}.")


# --------------------------------------------------------------------------- role, from the card

async def role_draft(session, actor: Employee, target: Employee, role: str):  # noqa: ANN001, ANN201
    """The /rol draft: nothing changes until the owner presses the one-time confirmation."""
    approval = await owners.request_role_change(session, actor, target.id, role)
    text = (f"Роль ўзгариши лойиҳаси: {target.code} {target.full_name}\n"
            f"{ROLE_LABELS.get(target.role, target.role)} → {ROLE_LABELS.get(role, role)}\n\n"
            f"Тасдиқ фақат шу ўзгариш учун, бир марта ва {settings.approval_ttl_minutes} дақиқа амал қилади.")
    buttons = inline([[("Тасдиқлаш", RoleCb(action="ok", emp_id=approval.id, role=role)),
                       ("Бекор", RoleCb(action="no", emp_id=approval.id))]])
    return text, buttons


@router.callback_query(PersonCb.filter(F.action == "role"))
async def pick_role(query: CallbackQuery, callback_data: PersonCb) -> None:
    async with session_scope() as session:
        actor = await staff_by_tg(session, query.from_user.id)
        emp = await session.get(Employee, callback_data.emp_id)
        if actor is None or actor.role != "owner" or emp is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
    roles = [r for r in ROLE_LABELS if r != emp.role]
    rows = [[(ROLE_LABELS[r], RoleCb(action="set", emp_id=emp.id, role=r)) for r in roles[i:i + 3]]
            for i in range(0, len(roles), 3)]
    await query.answer()
    await query.message.answer(T.say(actor, f"{emp.full_name} учун янги роль:"), reply_markup=inline(rows))


@router.callback_query(RoleCb.filter(F.action == "set"))
async def role_from_card(query: CallbackQuery, callback_data: RoleCb) -> None:
    async with session_scope() as session:
        actor = await staff_by_tg(session, query.from_user.id)
        target = await session.get(Employee, callback_data.emp_id)
        if actor is None or actor.role != "owner" or target is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        try:
            text, buttons = await role_draft(session, actor, target, callback_data.role)
        except owners.RoleError as exc:
            await query.answer(T.say(actor, str(exc)), show_alert=True)
            return
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(T.say(actor, text), reply_markup=buttons)


# --------------------------------------------------------------------------- someone without a card

@router.callback_query(JoinCb.filter(F.action == "add"))
async def add_start(query: CallbackQuery, callback_data: JoinCb, state: FSMContext) -> None:
    async with session_scope() as session:
        actor = await _manager(query, session)
        if actor is None:
            await query.answer(T.NOT_ALLOWED, show_alert=True)
            return
        bound = (await session.execute(select(Employee).where(
            Employee.telegram_user_id == callback_data.tg))).scalar_one_or_none()
    if bound is not None:
        await query.answer(T.say(actor, f"Бу Telegram аккаунт аллақачон {bound.code} {bound.full_name} "
                                        "карточкасига боғланган."), show_alert=True)
        return
    await state.clear()
    await state.set_state(AddPerson.position)
    await state.update_data(tg=callback_data.tg, lang=callback_data.lang if callback_data.lang in LANGS else "uz_cyrl")
    await query.answer()
    await query.message.answer(T.say(actor, f"Telegram ID {callback_data.tg} — янги ходим. "
                                            "Ҳар қадамда /bekor — бекор қилиш.\n\n1/4. Лавозими:"))


def _clean(message: Message, limit: int) -> str | None:
    value = " ".join((message.text or "").split())
    return value if value and len(value) <= limit and not value.startswith("/") else None


@router.message(AddPerson.position, F.text)
async def add_position(message: Message, state: FSMContext) -> None:
    value = _clean(message, 100)
    if value is None:
        await message.answer("Лавозимни 1–100 белгида ёзинг ёки /bekor:")
        return
    await state.update_data(position=value)
    await state.set_state(AddPerson.full_name)
    await message.answer("2/4. Тўлиқ ФИШи (камида фамилия ва исм):")


@router.message(AddPerson.full_name, F.text)
async def add_name(message: Message, state: FSMContext) -> None:
    value = _clean(message, 200)
    if value is None or len(value.split()) < 2:
        await message.answer("Камида фамилия ва исм керак. Қайта ёзинг ёки /bekor:")
        return
    await state.update_data(full_name=value)
    await state.set_state(AddPerson.department)
    async with session_scope() as session:
        depts = sorted({d for (d,) in (await session.execute(select(Employee.department))).all() if d})[:9]
    await state.update_data(depts=depts)
    rows = [[(d, PickCb(field="adddept", value=str(i))) for i, d in list(enumerate(depts))[j:j + 3]]
            for j in range(0, len(depts), 3)]
    await message.answer("3/4. Бўлими — тугмани босинг ёки янгисини ёзинг:",
                         reply_markup=inline(rows) if rows else None)


async def _ask_role(message: Message, state: FSMContext, actor_tg: int, department: str) -> None:
    await state.update_data(department=department)
    await state.set_state(AddPerson.role)
    async with session_scope() as session:
        actor = await staff_by_tg(session, actor_tg)
    roles = [r for r in ROLE_LABELS if r != "owner" or (actor and actor.role == "owner")]
    rows = [[(ROLE_LABELS[r], PickCb(field="addrole", value=r)) for r in roles[i:i + 3]]
            for i in range(0, len(roles), 3)]
    await message.answer(f"Бўлим: {department}\n4/4. Роли (HARI'да нима қила олади):", reply_markup=inline(rows))


@router.message(AddPerson.department, F.text)
async def add_department_typed(message: Message, state: FSMContext) -> None:
    value = _clean(message, 100)
    if value is None:
        await message.answer("Бўлимни 1–100 белгида ёзинг ёки /bekor:")
        return
    await _ask_role(message, state, message.from_user.id, value)


@router.callback_query(AddPerson.department, PickCb.filter(F.field == "adddept"))
async def add_department_picked(query: CallbackQuery, callback_data: PickCb, state: FSMContext) -> None:
    depts = (await state.get_data()).get("depts", [])
    index = int(callback_data.value) if callback_data.value.isdigit() else -1
    if not 0 <= index < len(depts):
        await query.answer(T.NOT_ALLOWED, show_alert=True)
        return
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await _ask_role(query.message, state, query.from_user.id, depts[index])


@router.callback_query(AddPerson.role, PickCb.filter(F.field == "addrole"))
async def add_role(query: CallbackQuery, callback_data: PickCb, state: FSMContext) -> None:
    if callback_data.value not in ROLE_LABELS:
        await query.answer(T.NOT_ALLOWED, show_alert=True)
        return
    await state.update_data(role=callback_data.value)
    await state.set_state(AddPerson.confirm)
    data = await state.get_data()
    await query.answer()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.message.answer(
        f"Текширинг:\nТелеграм ID: {data['tg']}\nФИШ: {data['full_name']}\nЛавозим: {data['position']}\n"
        f"Бўлим: {data['department']}\nРоль: {ROLE_LABELS[data['role']]}\nБошлаш санаси: бугун\n\n"
        "Сақласам, у дарҳол ходим бўлади ва менюси очилади.",
        reply_markup=inline([[("Сақлаш", PickCb(field="addok", value="yes")),
                              ("Бекор", PickCb(field="addok", value="no"))]]))


@router.callback_query(AddPerson.confirm, PickCb.filter(F.field == "addok"))
async def add_confirm(query: CallbackQuery, callback_data: PickCb, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    await query.message.edit_reply_markup(reply_markup=None)
    await query.answer()
    if callback_data.value != "yes":
        await query.message.answer(T.CANCELLED)
        return
    tg, role = int(data["tg"]), data.get("role")
    async with session_scope() as session:
        actor = await _manager(query, session)
        # Buttons can be forged: the role is checked again here, and only an owner makes an owner.
        if actor is None or role not in ROLE_LABELS or (role == "owner" and actor.role != "owner"):
            await query.message.answer(T.NOT_ALLOWED)
            return
        bound = (await session.execute(select(Employee).where(Employee.telegram_user_id == tg))).scalar_one_or_none()
        if bound is not None:
            await query.message.answer(f"Сақланмади: бу Telegram аккаунт аллақачон {bound.code} карточкасида.")
            return
        today = today_local()
        try:
            emp, created = await create_card(session, CardInput(
                full_name=data["full_name"], department=data["department"], position=data["position"],
                role=role, start_date=today, language=data.get("lang", "uz_cyrl"),
            ), created_by=actor.id)
        except CardError as exc:
            await query.message.answer(f"Сақланмади: {exc}")
            return
        if not created:
            await query.message.answer(f"Сақланмади: {emp.code} {emp.full_name} карточкаси аллақачон бор. "
                                       f"Унга /taklif {emp.code} билан ҳавола беринг.")
            return
        emp.telegram_user_id, emp.identity_verified_at, emp.status = tg, now_utc(), "active"
        await audit.record(session, "invite.use", "bound_by_owner", actor_id=actor.id, object_type="employee",
                           object_id=emp.id)
        await O.create_plan(session, emp, today, actor_id=actor.id)
        await session.flush()
        card, buttons = await profile_card(session, emp), card_buttons(actor, emp)
        welcome, menu = T.say(emp, T.welcome(emp, None, None)), main_menu(emp, await handles_work(session, emp))
    await query.message.answer(T.say(actor, f"Ходим қўшилди.\n\n{card}"), reply_markup=buttons)
    try:
        await bot.send_message(tg, welcome, reply_markup=menu)
    except TelegramAPIError as exc:
        logger.warning("Welcome to a new card not delivered: {}", type(exc).__name__)
        await query.message.answer("Ходимга хабар етмади: у HARI'ни қайта очиб /start босиши керак.")
