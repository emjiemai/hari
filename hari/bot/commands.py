"""A small Menu button: at most four commands, the ones each kind of person uses most, in their language.
Everything else is still done by writing. Someone without a card (an applicant, a stranger) sees no menu.

- HR and owners: tasks, applications, what waits for them (step results and requests), staff.
- Mentors and managers: what waits for them, tasks, their plan, a request.
- Employees: tasks, plan, a request, their requests.
The profile and a new employee are not in it: «профилим», «янги ходим» are simply written.

Each person's menu is set at start-up and again whenever what they should see changes (a new link,
a new role): `refresh` is called on every message and only talks to Telegram when the kind changed.
"""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, MenuButtonCommands
from loguru import logger
from sqlalchemy import select

from hari.access import can_manage_employees, is_staff
from hari.bot.common import handles_work
from hari.db.models import Employee
from hari.db.session import session_scope

# (command, uz_cyrl, uz_latn, ru)
MENUS: dict[str, list[tuple[str, str, str, str]]] = {
    "manage": [("topshiriqlar", "Топшириқлар: кимга нима берилган, қандай кетяпти",
                "Topshiriqlar: kimga nima berilgan, qanday ketyapti", "Задачи: кому что дано и как идёт"),
               ("arizalar", "Аризалар", "Arizalar", "Заявки"),
               ("navbat", "Мени кутаётганлар: натижалар, мурожаатлар", "Meni kutayotganlar: natijalar, murojaatlar",
                "Ждут меня: результаты, обращения"),
               ("xodimlar", "Ходимлар", "Xodimlar", "Сотрудники")],
    "work": [("navbat", "Мени кутаётганлар: натижалар, мурожаатлар", "Meni kutayotganlar: natijalar, murojaatlar",
              "Ждут меня: результаты, обращения"),
             ("topshiriqlar", "Топшириқларим", "Topshiriqlarim", "Мои задачи"),
             ("reja", "Мослашув режам", "Moslashuv rejam", "Мой план адаптации"),
             ("murojaat", "Мурожаат ёзиш", "Murojaat yozish", "Написать обращение")],
    "employee": [("topshiriqlar", "Топшириқларим", "Topshiriqlarim", "Мои задачи"),
                 ("reja", "Мослашув режам", "Moslashuv rejam", "Мой план адаптации"),
                 ("murojaat", "Мурожаат ёзиш", "Murojaat yozish", "Написать обращение"),
                 ("murojaatlarim", "Мурожаатларим", "Murojaatlarim", "Мои обращения")],
}
_LANG_INDEX = {"uz_cyrl": 1, "uz_latn": 2, "ru": 3}
_set: dict[int, str] = {}  # chat → the menu last set there (kind and language), so Telegram is asked only on change


def kind_of(emp: Employee, works: bool) -> str:
    if can_manage_employees(emp):
        return "manage"
    return "work" if works else "employee"


def commands_for(kind: str, language: str) -> list[BotCommand]:
    index = _LANG_INDEX.get(language, 1)
    return [BotCommand(command=row[0], description=row[index]) for row in MENUS[kind]]


async def refresh(bot: Bot, emp: Employee, works: bool) -> None:
    """Make this person's menu match who they are now. Cheap when nothing changed."""
    if not emp.telegram_user_id:
        return
    key = f"{kind_of(emp, works)}:{emp.language}"
    if _set.get(emp.telegram_user_id) == key:
        return
    try:
        await bot.set_my_commands(commands_for(kind_of(emp, works), emp.language),
                                  scope=BotCommandScopeChat(chat_id=emp.telegram_user_id))
    except TelegramAPIError as exc:
        logger.warning("Menu of one chat not set: {}", type(exc).__name__)
        return
    _set[emp.telegram_user_id] = key


async def install(bot: Bot) -> None:
    """At start-up: no menu for people without a card, each linked person's own menu."""
    _set.clear()
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError as exc:
        logger.warning("Default menu not set: {}", type(exc).__name__)
    async with session_scope() as session:
        people = [(e, await handles_work(session, e)) for e in (await session.execute(
            select(Employee).where(Employee.telegram_user_id.is_not(None)))).scalars() if is_staff(e)]
    for emp, works in people:
        await refresh(bot, emp, works)
    logger.info("Menus set ({} linked chats)", len(people))
