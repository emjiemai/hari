"""Sending messages. HARI shows no buttons: people write what they want (see hari.intent)."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ReplyKeyboardRemove
from loguru import logger

from hari.bot import texts as T
from hari.db.models import Employee

# Sent once with a reply: takes the old menu keyboard off a phone that still shows it.
NO_KEYBOARD = ReplyKeyboardRemove()


async def notify(bot: Bot, person: Employee | None, text: str) -> bool:
    """Message a staff member; never crash a flow because one person blocked the bot."""
    if person is None or not person.telegram_user_id:
        return False
    try:
        await bot.send_message(person.telegram_user_id, T.say(person, text))
        return True
    except TelegramAPIError as exc:
        logger.warning("Message to {} not delivered: {}", person.code, type(exc).__name__)
        return False


async def send_proof(bot: Bot, person: Employee, proof_file_id: str | None) -> None:
    if not proof_file_id or not person.telegram_user_id:
        return
    kind, _, file_id = proof_file_id.partition(":")
    try:
        if kind == "photo":
            await bot.send_photo(person.telegram_user_id, file_id)
        else:
            await bot.send_document(person.telegram_user_id, file_id)
    except TelegramAPIError as exc:
        logger.warning("Proof file to {} not delivered: {}", person.code, type(exc).__name__)
