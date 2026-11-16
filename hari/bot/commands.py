"""The command list behind Telegram's "Menu" button (left of the message field).

Each person sees only what they may use: strangers and applicants get /start and /help; employees
their own commands; HR and owners also the staff commands; owners /rol. The list is set per chat,
so it follows the role: at start-up for everyone, and again on /start, on joining and on a role change.
The handlers still check every right themselves — the list is only a menu.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from loguru import logger
from sqlalchemy import select

from hari.access import can_manage_employees, is_staff
from hari.bot import texts as T
from hari.bot.common import handles_work
from hari.db.models import Employee
from hari.db.session import session_scope

EVERYONE = [("start", "Бошлаш"), ("help", "Ёрдам")]
EMPLOYEE = [
    ("reja", "Мослашув режам"),
    ("murojaat", "HR'га мурожаат юбориш"),
    ("murojaatlarim", "Мурожаатларим"),
    ("profilim", "Профилим"),
    ("unut", "Суҳбат хотирасини ўчириш"),
    ("help", "Ёрдам"),
]
WORK = [("navbat", "Иш навбати: менга келган ишлар")]
HR = [
    ("yangi", "Янги ходим карточкаси"),
    ("xodimlar", "Ходимлар рўйхати"),
    ("profil", "Ходим профили: /profil E-0003"),
    ("taklif", "Янги таклиф ҳаволаси: /taklif E-0003"),
]
OWNER = [("rol", "Ролни ўзгартириш: /rol E-0003 hr")]


def commands_for(emp: Employee | None, works: bool = False) -> list[tuple[str, str]]:
    if emp is None or not is_staff(emp):
        return EVERYONE
    items = list(EMPLOYEE)
    if works:
        items += WORK
    if can_manage_employees(emp):
        items += HR
    if emp.role == "owner":
        items += OWNER
    return items


def _commands(emp: Employee | None, items: list[tuple[str, str]]) -> list[BotCommand]:
    return [BotCommand(command=name, description=T.say(emp, text)) for name, text in items]


async def sync_for(bot: Bot, emp: Employee) -> None:
    """Set this person's menu to match their role now. Never stops a flow if Telegram refuses."""
    if not emp.telegram_user_id:
        return
    async with session_scope() as session:
        fresh = await session.get(Employee, emp.id)
        works = fresh is not None and is_staff(fresh) and await handles_work(session, fresh)
    scope = BotCommandScopeChat(chat_id=emp.telegram_user_id)
    try:
        if fresh is None or not is_staff(fresh):
            await bot.delete_my_commands(scope=scope)  # back to the default list
        else:
            await bot.set_my_commands(_commands(fresh, commands_for(fresh, works)), scope=scope)
    except TelegramAPIError as exc:
        logger.warning("Command menu for {} not set: {}", emp.code, type(exc).__name__)


async def install(bot: Bot) -> None:
    """At start-up: the default list, then each linked person's own."""
    try:
        await bot.set_my_commands(_commands(None, EVERYONE), scope=BotCommandScopeDefault())
    except TelegramAPIError as exc:
        logger.warning("Default command menu not set: {}", type(exc).__name__)
        return
    async with session_scope() as session:
        people = (await session.execute(
            select(Employee).where(Employee.telegram_user_id.is_not(None)))).scalars().all()
    for emp in people:
        await sync_for(bot, emp)
    logger.info("Command menu set for {} linked people", len(people))
