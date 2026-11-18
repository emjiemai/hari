"""HARI has no Menu button: people write what they want. At start-up this removes the command list an
earlier version set (the default one and each person's own), so the Menu button disappears for everyone.
Slash commands still work when typed; they are just not shown."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommandScopeChat, BotCommandScopeDefault, MenuButtonDefault
from loguru import logger
from sqlalchemy import select

from hari.db.models import Employee
from hari.db.session import session_scope


async def install(bot: Bot) -> None:
    try:
        await bot.delete_my_commands(scope=BotCommandScopeDefault())
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    except TelegramAPIError as exc:
        logger.warning("Command menu not removed: {}", type(exc).__name__)
        return
    async with session_scope() as session:
        chats = list((await session.execute(
            select(Employee.telegram_user_id).where(Employee.telegram_user_id.is_not(None)))).scalars())
    for chat_id in chats:
        try:
            await bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id))
        except TelegramAPIError as exc:
            logger.warning("Command menu of one chat not removed: {}", type(exc).__name__)
    logger.info("Command menu removed ({} linked chats)", len(chats))
