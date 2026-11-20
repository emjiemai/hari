"""Owners see every newcomer and run each card by writing.

- Someone opens HARI through their invite link: every owner gets the full profile card.
- Someone without a card opens HARI (or a link that no longer works): every owner gets what Telegram
  tells about them, once per account. Writing «<Telegram ID> ни ходим қилиб қўш» makes them an employee
  (position, name, department, role). The owner vouches for who it is; the Telegram ID is the proof.
- «E-0003» or «E-0003 профили» shows any card; «E-0003 лавозими: …» / «… бўлими: …» change it (owner or
  HR); «E-0003 ролини hr қил» changes the role (owner only, through the one-time confirmation).
"""

from __future__ import annotations

import hashlib

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, User
from loguru import logger
from sqlalchemy import select

from hari import approvals, audit, owners, voice
from hari import intent as I
from hari import onboarding as O
from hari.access import can_manage_employees, is_staff
from hari.apply.report import vacancy_title
from hari.apply.texts import STATUS_LABELS as APP_STATUS
from hari.bot import texts as T
from hari.bot.common import staff_by_tg
from hari.bot.context import Ctx, ask_confirm, ask_pick
from hari.bot.handlers.staff import ROLE_LABELS, ROLE_WORDS, _cancelled, _numbered
from hari.bot.ui import NO_KEYBOARD, notify
from hari.config import settings
from hari.db.models import Application, AuditEvent, Employee
from hari.db.session import session_scope
from hari.employees import CardError, CardInput, create_card
from hari.translit import normalize
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


def card_hint(viewer: Employee, emp: Employee) -> str:
    """What the viewer can write to change the card (there are no buttons)."""
    lines = [f"Ўзгартириш учун ёзинг: «{emp.code} лавозими: …» ёки «{emp.code} бўлими: …»."]
    if viewer.role == "owner":
        lines.append(f"Роль: «{emp.code} ролини hr қил» (роллар: {', '.join(ROLE_LABELS)}).")
    return "\n".join(lines)


async def report_join(bot: Bot, emp_id: str) -> None:
    """Someone opened their invite link: every owner sees the whole card and how to change it."""
    async with session_scope() as session:
        emp = await session.get(Employee, emp_id)
        if emp is None:
            return
        card = await profile_card(session, emp)
        targets = [(o, card_hint(o, emp)) for o in await owners_to_tell(session, exclude_id=emp.id)]
    for owner, hint in targets:
        await notify(bot, owner, f"Янги ходим HARI'га қўшилди.\n\n{card}\n\n{hint}")


def _seen_key(telegram_user_id: int) -> str:
    # The audit row marks "already reported" without writing the raw Telegram ID to the log.
    return hashlib.sha256(f"stranger:{telegram_user_id}".encode()).hexdigest()[:40]


async def report_stranger(bot: Bot, user: User, why: str = "") -> None:
    """Someone with no card opened HARI. Owners hear about each account once."""
    key = _seen_key(user.id)
    lang = "ru" if (user.language_code or "").startswith("ru") else "uz_cyrl"
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
        # Their Telegram app's language is kept, so a card made from this report starts in a language they read.
        await audit.record(session, "stranger.report", "sent", object_type="telegram", object_id=key, detail=lang)
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
        f"Ходимингиз бўлса, «{user.id} ни ходим қилиб қўш» деб ёзинг: лавозими, исми, бўлими ва роли сўралади. "
        "Танимасангиз, ҳеч нарса қилманг — унинг ҳеч қандай ҳуқуқи йўқ.",
    ])
    for owner in targets:
        await notify(bot, owner, text)


async def _stranger_language(session, telegram_user_id: int) -> str:  # noqa: ANN001
    row = (await session.execute(select(AuditEvent.detail).where(
        AuditEvent.action == "stranger.report", AuditEvent.object_id == _seen_key(telegram_user_id)).limit(1))).first()
    return row[0] if row and row[0] in LANGS else "uz_cyrl"


# --------------------------------------------------------------------------- finding a person by what was written

async def find_person(ctx: Ctx, args: dict, action: str, question: str) -> Employee | None:
    """args["who"]: a code (E-0003), a Telegram ID, or a name; else the names in the message. Several
    people fit → HARI lists them and the typed number decides. Only HR and owners get here."""
    async with session_scope() as session:
        actor = await _manager(ctx.message, session)
        if actor is None:
            await ctx.say(T.NOT_ALLOWED)
            return None
        if args.get("emp_id"):
            return await session.get(Employee, args["emp_id"])
        who = args.get("who")
        who = str(who).strip() if who not in (None, "") else ""
        code = I.employee_code(who) or (I.employee_code(ctx.text) if not who else None)
        if code:
            emp = (await session.execute(select(Employee).where(Employee.code == code))).scalar_one_or_none()
            if emp is None:
                await ctx.say("Ходим топилмади.")
            return emp
        if who.isdigit():
            emp = (await session.execute(select(Employee).where(
                Employee.telegram_user_id == int(who)))).scalar_one_or_none()
            if emp is None:
                await ctx.say("Ходим топилмади.")
            return emp
        everyone = list((await session.execute(select(Employee).where(Employee.status != "archived")
                                               .order_by(Employee.code))).scalars())
    text = normalize((who or ctx.text).lower())
    fits = [p for p in everyone if any(len(w) >= 4 and normalize(w.lower()) in text for w in p.full_name.split())]
    if len(fits) == 1:
        return fits[0]
    if not fits:
        await ctx.say("Ходим топилмади. Кодини ёзинг, масалан: E-0003 (рўйхат: «ходимлар»).")
        return None
    await ask_pick(ctx, question, action, {k: v for k, v in args.items() if k != "who"}, "emp_id",
                   [(p.id, f"{p.code} {p.full_name} — {p.position}") for p in fits[:10]])
    return None


# --------------------------------------------------------------------------- the card

async def employee_profile(ctx: Ctx, args: dict) -> None:
    emp = await find_person(ctx, args, "employee_profile", "Кимнинг профили?")
    if emp is None:
        return
    async with session_scope() as session:
        card = await profile_card(session, await session.get(Employee, emp.id))
    await ctx.say(f"{card}\n\n{card_hint(ctx.emp, emp)}")


async def employee_edit(ctx: Ctx, args: dict) -> None:
    emp = await find_person(ctx, args, "employee_edit", "Кимнинг карточкаси?")
    if emp is None:
        return
    field = {"position": "pos", "department": "dept"}.get(args.get("field") or "", args.get("field"))
    if field not in FIELDS:
        await ctx.say(f"Нимани ўзгартирай: лавозимими ёки бўлимими? Масалан: «{emp.code} лавозими: Сотувчи».")
        return
    attr, label = FIELDS[field]
    value = " ".join(str(args.get("value") or "").split())
    if not value:
        await ctx.state.set_state(EditPerson.value)
        await ctx.state.update_data(emp_id=emp.id, field=field)
        await ctx.say(f"{emp.full_name}: ҳозирги {label} — «{getattr(emp, attr)}».\n"
                      f"Янги {label}ни ёзинг («бекор» — тўхтатиш):")
        return
    if len(value) > 100:
        await ctx.say("1–100 белги бўлсин.")
        return
    if not ctx.confirmed:
        await ask_confirm(ctx, f"{emp.code} {emp.full_name}: {label} «{getattr(emp, attr)}» → «{value}».",
                          "employee_edit", {"emp_id": emp.id, "field": field, "value": value})
        return
    await _save_field(ctx.message, ctx.bot, emp.id, field, value)


@router.message(EditPerson.value, F.text)
async def edit_value_typed(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    value = " ".join(message.text.split())
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    if not value or len(value) > 100 or value.startswith("/"):
        await message.answer("1–100 белги бўлсин. Қайта ёзинг ёки «бекор»:")
        return
    await state.clear()
    await _save_field(message, bot, data.get("emp_id", ""), data.get("field"), value)


async def _save_field(message: Message, bot: Bot, emp_id: str, field: str | None, value: str) -> None:
    attr, label = FIELDS.get(field or "", (None, None))
    async with session_scope() as session:
        actor = await _manager(message, session)
        emp = await session.get(Employee, emp_id)
        if actor is None or emp is None or attr is None:
            await message.answer(T.NOT_ALLOWED)
            return
        old = getattr(emp, attr)
        setattr(emp, attr, value)
        await audit.record(session, "employee.edit", "changed", actor_id=actor.id, object_type="employee",
                           object_id=emp.id, detail=attr)
        await session.flush()
        card = await profile_card(session, emp)
        person = emp if emp.id != actor.id else None
    await message.answer(T.say(actor, f"Сақланди: {label} «{old}» → «{value}».\n\n{card}"))
    await notify(bot, person, f"Профилингиз янгиланди: {label} — {value}.")


# --------------------------------------------------------------------------- role (owner, one-time approval)

APPROVAL_REASONS = {"expired": "тасдиқ муддати ўтди", "payload_changed": "ўзгариш мазмуни бошқа",
                    "version_changed": "ходим ёзуви бу орада ўзгарган", "already_used": "тасдиқ аллақачон ишлатилган",
                    "other_actor": "бу тасдиқ сизники эмас"}


async def role_change(ctx: Ctx, args: dict) -> None:
    """An owner drafts a role change; it happens only when they answer «ҳа» within the approval's time
    (spec 2.0 §25, T19–T20). The «ҳа» uses the approval made for exactly this change."""
    if ctx.emp.role != "owner":
        await ctx.say(T.NOT_ALLOWED)
        return
    if args.get("approval_id"):
        if ctx.confirmed:
            await _role_confirm(ctx, args)
        return
    target = await find_person(ctx, args, "role_change", "Кимнинг роли?")
    if target is None:
        return
    role = args.get("role")
    if role not in ROLE_LABELS and role:
        role = I.choice(str(role), [(r, (ROLE_LABELS[r], *ROLE_WORDS[r])) for r in ROLE_LABELS])
    if role not in ROLE_LABELS:
        await ctx.say(f"{target.full_name} учун қайси роль? Масалан: «{target.code} ролини hr қил».\nРоллар: "
                      + ", ".join(f"{k} ({v})" for k, v in ROLE_LABELS.items()))
        return
    async with session_scope() as session:
        actor = await session.get(Employee, ctx.emp.id)
        target = await session.get(Employee, target.id)
        try:
            approval = await owners.request_role_change(session, actor, target.id, role)
        except owners.RoleError as exc:
            await ctx.say(str(exc))
            return
        summary = (f"Роль ўзгариши лойиҳаси: {target.code} {target.full_name}\n"
                   f"{ROLE_LABELS.get(target.role, target.role)} → {ROLE_LABELS.get(role, role)}\n\n"
                   f"Тасдиқ фақат шу ўзгариш учун, бир марта ва {settings.approval_ttl_minutes} дақиқа амал қилади.")
        pending = {"approval_id": approval.id, "target_id": target.id, "role": role}
    await ask_confirm(ctx, summary, "role_change", pending)


async def _role_confirm(ctx: Ctx, args: dict) -> None:
    try:
        async with session_scope() as session:
            actor = await session.get(Employee, ctx.emp.id)
            try:
                target = await owners.confirm_role_change(session, actor, args["approval_id"], args["target_id"],
                                                          args["role"])
            except approvals.ApprovalError as exc:
                await ctx.say(f"Бажарилмади: {APPROVAL_REASONS.get(exc.reason, exc.reason)}. Қайта сўранг.")
                return
            done = f"{target.code} {target.full_name}: роль — {ROLE_LABELS.get(target.role, target.role)}."
    except owners.RoleError as exc:
        await ctx.say(str(exc))
        return
    await ctx.say(done)


# --------------------------------------------------------------------------- someone without a card

async def add_person(ctx: Ctx, args: dict) -> None:
    tg = args.get("tg")
    tg = int(tg) if isinstance(tg, int) or (isinstance(tg, str) and tg.isdigit()) else I.telegram_id(ctx.text)
    if not tg:
        await ctx.say("Telegram ID'сини ёзинг, масалан: «123456789 ни ходим қилиб қўш».")
        return
    async with session_scope() as session:
        actor = await _manager(ctx.message, session)
        if actor is None:
            await ctx.say(T.NOT_ALLOWED)
            return
        bound = (await session.execute(select(Employee).where(Employee.telegram_user_id == tg))).scalar_one_or_none()
        lang = await _stranger_language(session, tg)
    if bound is not None:
        await ctx.say(f"Бу Telegram аккаунт аллақачон {bound.code} {bound.full_name} карточкасига боғланган.")
        return
    await ctx.state.clear()
    await ctx.state.set_state(AddPerson.position)
    await ctx.state.update_data(tg=tg, lang=lang)
    await ctx.say(f"Telegram ID {tg} — янги ходим. Ҳар қадамда «бекор» деб ёзсангиз, тўхтатаман.\n\n1/4. Лавозими:")


def _clean(message: Message, limit: int) -> str | None:
    value = " ".join((message.text or "").split())
    return value if value and len(value) <= limit and not value.startswith("/") else None


def _roles_for(actor: Employee | None) -> list[str]:
    return [r for r in ROLE_LABELS if r != "owner" or (actor is not None and actor.role == "owner")]


@router.message(AddPerson.position, F.text)
async def add_position(message: Message, state: FSMContext) -> None:
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    value = _clean(message, 100)
    if value is None:
        await message.answer("Лавозимни 1–100 белгида ёзинг ёки «бекор»:")
        return
    await state.update_data(position=value)
    await state.set_state(AddPerson.full_name)
    await message.answer("2/4. Тўлиқ ФИШи (камида фамилия ва исм):")


@router.message(AddPerson.full_name, F.text)
async def add_name(message: Message, state: FSMContext) -> None:
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    value = _clean(message, 200)
    if value is None or len(value.split()) < 2:
        await message.answer("Камида фамилия ва исм керак. Қайта ёзинг ёки «бекор»:")
        return
    await state.update_data(full_name=value)
    await state.set_state(AddPerson.department)
    async with session_scope() as session:
        depts = sorted({d for (d,) in (await session.execute(select(Employee.department))).all() if d})[:9]
    await state.update_data(depts=depts)
    listing = ("\n" + _numbered(depts)) if depts else ""
    await message.answer("3/4. Бўлими — рўйхатдаги рақамни ёки бўлим номини ёзинг:" + listing)


@router.message(AddPerson.department, F.text)
async def add_department(message: Message, state: FSMContext) -> None:
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    value = _clean(message, 100)
    if value is None:
        await message.answer("Бўлимни 1–100 белгида ёзинг ёки «бекор»:")
        return
    depts = (await state.get_data()).get("depts", [])
    if value.isdigit():
        index = int(value) - 1
        if not 0 <= index < len(depts):
            await message.answer("Бундай рақам рўйхатда йўқ. Рақамни ёки бўлим номини ёзинг:")
            return
        value = depts[index]
    await state.update_data(department=value)
    await state.set_state(AddPerson.role)
    async with session_scope() as session:
        actor = await staff_by_tg(session, message.from_user.id)
    await message.answer(f"Бўлим: {value}\n4/4. Роли (HARI'да нима қила олади) — рақамини ёки номини ёзинг:\n"
                         + _numbered([ROLE_LABELS[r] for r in _roles_for(actor)]))


@router.message(AddPerson.role, F.text)
async def add_role(message: Message, state: FSMContext) -> None:
    if _cancelled(message):
        await state.clear()
        await message.answer(T.CANCELLED)
        return
    async with session_scope() as session:
        actor = await staff_by_tg(session, message.from_user.id)
    roles = _roles_for(actor)
    role = I.choice(message.text, [(r, (ROLE_LABELS[r], *ROLE_WORDS[r])) for r in roles])
    if role is None:
        await message.answer("Тушунмадим. Ролни рақами билан ёзинг:\n" + _numbered([ROLE_LABELS[r] for r in roles]))
        return
    await state.update_data(role=role)
    await state.set_state(AddPerson.confirm)
    data = await state.get_data()
    await message.answer(
        f"Текширинг:\nТелеграм ID: {data['tg']}\nФИШ: {data['full_name']}\nЛавозим: {data['position']}\n"
        f"Бўлим: {data['department']}\nРоль: {ROLE_LABELS[role]}\nБошлаш санаси: бугун\n\n"
        "Сақласам, у дарҳол ходим бўлади. Сақлайми? «ҳа» ёки «йўқ» деб ёзинг.")


@router.message(AddPerson.confirm, F.text)
async def add_confirm(message: Message, state: FSMContext, bot: Bot) -> None:
    if not I.is_yes(message.text):
        if I.is_no(message.text):
            await state.clear()
            await message.answer(T.CANCELLED)
        else:
            await message.answer("Сақлаш учун «ҳа», бекор қилиш учун «йўқ» деб ёзинг.")
        return
    data = await state.get_data()
    await state.clear()
    tg, role = int(data["tg"]), data.get("role")
    async with session_scope() as session:
        actor = await _manager(message, session)
        # Checked again at saving: only HR or an owner, and only an owner makes an owner.
        if actor is None or role not in ROLE_LABELS or (role == "owner" and actor.role != "owner"):
            await message.answer(T.NOT_ALLOWED)
            return
        bound = (await session.execute(select(Employee).where(Employee.telegram_user_id == tg))).scalar_one_or_none()
        if bound is not None:
            await message.answer(f"Сақланмади: бу Telegram аккаунт аллақачон {bound.code} карточкасида.")
            return
        today = today_local()
        try:
            emp, created = await create_card(session, CardInput(
                full_name=data["full_name"], department=data["department"], position=data["position"],
                role=role, start_date=today, language=data.get("lang", "uz_cyrl"),
            ), created_by=actor.id)
        except CardError as exc:
            await message.answer(f"Сақланмади: {exc}")
            return
        if not created:
            await message.answer(f"Сақланмади: {emp.code} {emp.full_name} карточкаси аллақачон бор. "
                                 f"Унга ҳавола беринг: «{emp.code} га ҳавола».")
            return
        emp.telegram_user_id, emp.identity_verified_at, emp.status = tg, now_utc(), "active"
        await audit.record(session, "invite.use", "bound_by_owner", actor_id=actor.id, object_type="employee",
                           object_id=emp.id)
        await O.create_plan(session, emp, today, actor_id=actor.id)
        await session.flush()
        card = await profile_card(session, emp)
        welcome = T.welcome(emp, None, None)
    welcome = T.say(emp, await voice.natural(welcome, [emp.full_name, *emp.full_name.split()]))
    await message.answer(T.say(actor, f"Ходим қўшилди.\n\n{card}\n\n{card_hint(actor, emp)}"))
    try:
        await bot.send_message(tg, welcome, reply_markup=NO_KEYBOARD)
    except TelegramAPIError as exc:
        logger.warning("Welcome to a new card not delivered: {}", type(exc).__name__)
        await message.answer("Ходимга хабар етмади: у HARI'ни қайта очиб /start босиши керак.")


ACTIONS = {
    "employee_profile": employee_profile, "employee_edit": employee_edit, "role_change": role_change,
    "add_person": add_person,
}
