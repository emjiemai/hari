"""Remove the bot's webhook so the bot can run in polling mode on a PC again.

    python -m scripts.unset_webhook

Only do this when the Render service is stopped (or uses another token): while the webhook is
gone, the live bot on Render receives nothing until it restarts and sets it again.
"""

from __future__ import annotations

import asyncio
import sys

from aiogram import Bot

from hari.config import settings


async def run() -> None:
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    try:
        info = await bot.get_webhook_info()
        if not info.url:
            print("No webhook is set. Polling mode will work.")
            return
        await bot.delete_webhook(drop_pending_updates=False)
        print(f"Webhook removed ({info.url}). Waiting updates are kept for the next start.")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run())
