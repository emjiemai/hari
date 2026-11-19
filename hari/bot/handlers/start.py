"""/start: binding a Telegram account through a one-time link, and the welcome. /bekor stops any form."""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from hari import agenda
from hari.bot import texts as T
from hari.bot.common import staff_by_tg
from hari.bot.handlers.people import report_join, report_stranger
from hari.bot.ui import NO_KEYBOARD, notify
from hari.db.models import Employee
from hari.db.session import session_scope
from hari.employees import BindError, bind_telegram
from hari.owners import claim_owner_name
from hari.worktime import now_utc

router = Router(name="start")


async def _welcome(message: Message, emp: Employee) -> None:
    async with session_scope() as session:
        emp = await session.get(Employee, emp.id)
        manager = (await session.get(Employee, emp.manager_id)).full_name if emp.manager_id else None
        mentor = (await session.get(Employee, emp.mentor_id)).full_name if emp.mentor_id else None
    # NO_KEYBOARD takes the old menu keyboard off the phone, if an earlier version left one there.
    await message.answer(T.say(emp, T.welcome(emp, manager, mentor)), reply_markup=NO_KEYBOARD)


@router.message(CommandStart(deep_link=True))
async def start_with_token(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    token = (command.args or "").strip()
    try:
        async with session_scope() as session:
            emp = await bind_telegram(session, token, message.from_user.id)
    except BindError as exc:
        await message.answer(T.INVITE_TAKEN if str(exc) == "account_taken" else T.INVITE_INVALID,
                             reply_markup=NO_KEYBOARD)
        if str(exc) != "account_taken":
            await report_stranger(bot, message.from_user, " (ишламайдиган ҳавола билан)")
        return
    await _welcome(message, emp)
    await _start_the_day(message, bot, emp)
    await report_join(bot, emp.id)


async def _start_the_day(message: Message, bot: Bot, emp: Employee) -> None:
    """A newcomer just joined: HARI says at once what to do first, and tells their mentor and manager
    that the person is here and what is expected of them — nobody has to ask."""
    async with session_scope() as session:
        emp = await session.get(Employee, emp.id)
        blocks = await agenda.todo(session, emp, now_utc())
        mentor = await session.get(Employee, emp.mentor_id) if emp.mentor_id else None
        manager = await session.get(Employee, emp.manager_id) if emp.manager_id else None
    if blocks:
        await message.answer(T.say(emp, "Бугун шундан бошлаймиз:\n\n" + "\n\n".join(blocks)))
    for person, role in ((mentor, "мураббийи"), (manager, "раҳбари")):
        if person is not None and person.id != emp.id:
            await notify(bot, person, f"{emp.full_name} ({emp.position}) HARI'га қўшилди, сиз унинг {role}сиз. "
                                      "Унинг мослашув режасидаги учрашувлар ва натижаларни сиз қабул қиласиз: натижа "
                                      "келганда шу ерга ёзаман. Биринчи кунида у билан учрашиб, режани келишиб олинг.")


@router.message(CommandStart())
async def start_plain(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    async with session_scope() as session:
        user = message.from_user
        await claim_owner_name(session, user.id, user.first_name, user.last_name)
        emp = await staff_by_tg(session, user.id)
    if emp is None:
        await message.answer(T.UNKNOWN_USER.format(tg_id=message.from_user.id), reply_markup=NO_KEYBOARD)
        await report_stranger(bot, message.from_user)
        return
    await _welcome(message, emp)


@router.message(Command("bekor", "cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    async with session_scope() as session:
        emp = await staff_by_tg(session, message.from_user.id)
    await message.answer(T.say(emp, T.CANCELLED))
