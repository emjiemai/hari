"""Start HARI: database, knowledge base, Telegram connection and the scheduler.

    python -m hari.main

BOT_MODE=polling (default, a local PC): HARI asks Telegram for new messages.
BOT_MODE=webhook (Render): a small web server on $PORT; Telegram posts every update to
WEBHOOK_PATH with a secret header, and Render checks /health.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from loguru import logger

from hari import scheduler
from hari.apply.vacancies import seed_vacancies
from hari.bot import common
from hari.bot.handlers import apply, employee, hiring, staff, start
from hari.bot.middleware import DedupeMiddleware
from hari.config import WEBHOOK_PATH, settings
from hari.db.session import create_schema, ping, session_scope
from hari.knowledge.loader import load_directory
from hari.owners import ensure_owners

_dispatcher: Dispatcher | None = None


def build_dispatcher() -> Dispatcher:
    """One dispatcher per process: aiogram routers can be attached to a single parent only."""
    global _dispatcher
    if _dispatcher is None:
        dp = Dispatcher(storage=MemoryStorage())
        dp.update.outer_middleware(DedupeMiddleware())
        dp.include_router(apply.router)  # first: applicants; its filters pass staff and strangers through
        dp.include_router(start.router)
        dp.include_router(staff.router)
        dp.include_router(hiring.router)
        dp.include_router(employee.router)  # last: its catch-all answers questions
        _dispatcher = dp
    return _dispatcher


async def prepare() -> None:
    await create_schema()
    async with session_scope() as session:
        result = await load_directory(session, settings.knowledge_dir)
        added = await seed_vacancies(session)
        owners = await ensure_owners(session, settings.owner_ids)
    if added:
        logger.info("Vacancies: loaded the {} starting vacancies", added)
    if owners:
        logger.info("Owners from HARI_OWNERS: {}", ", ".join(owners))
    logger.info("Knowledge: {} loaded, {} unchanged, {} refused, {} retired",
                len(result.loaded), len(result.unchanged), len(result.refused), len(result.retired))
    for name, why in result.refused:
        logger.warning("Refused {}: {}", name, why)
    if not settings.openrouter_api_key.get_secret_value():
        logger.warning("OPENROUTER_API_KEY is empty: answers use the approved excerpt only (no AI text)")


# --------------------------------------------------------------------------- web server (webhook mode)

async def health(_request: web.Request) -> web.Response:
    ok = await ping()
    return web.json_response({"status": "ok" if ok else "database unavailable"}, status=200 if ok else 503)


def build_web_app(bot: Bot, dp: Dispatcher) -> web.Application:
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    # Answers Telegram at once and handles the update in the background, so a slow AI answer
    # never makes Telegram resend it; a resent one is dropped by DedupeMiddleware anyway.
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=settings.telegram_secret_token).register(
        app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)
    return app


async def run_webhook(bot: Bot, dp: Dispatcher) -> None:
    if not settings.webhook_url:
        raise SystemExit("BOT_MODE=webhook needs a public address: WEBHOOK_BASE_URL (Render sets RENDER_EXTERNAL_URL)")
    runner = web.AppRunner(build_web_app(bot, dp))
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=settings.port).start()
    await bot.set_webhook(settings.webhook_url, secret_token=settings.telegram_secret_token,
                          allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=False)
    logger.info("Webhook mode: listening on port {}, Telegram posts to {}", settings.port, settings.webhook_url)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):  # Render sends SIGTERM before a restart or deploy
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: Ctrl+C still stops the process
            pass
    timers = asyncio.create_task(scheduler.run_forever(bot))
    try:
        await stop.wait()
    finally:
        logger.info("Stopping")
        timers.cancel()
        await runner.cleanup()  # the webhook stays set: the next start picks up waiting updates


# --------------------------------------------------------------------------- polling (local PC)

async def run_polling(bot: Bot, dp: Dispatcher) -> None:
    info = await bot.get_webhook_info()
    if info.url:
        # This token is being served by a webhook (Render). Polling would take it over and stop the live bot.
        raise SystemExit(
            f"This bot token has a webhook ({info.url}) — it is probably running on Render. "
            "Use a separate test bot token on your PC, or stop the Render service and run "
            "`python -m scripts.unset_webhook` first."
        )
    timers = asyncio.create_task(scheduler.run_forever(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        timers.cancel()


async def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level="INFO")
    if settings.log_file:
        logger.add(settings.log_file, rotation="5 MB", retention=10, level="INFO", encoding="utf-8")
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set")
    mode = settings.bot_mode.strip().lower()
    if mode not in ("polling", "webhook"):
        raise SystemExit(f"BOT_MODE must be polling or webhook, not {settings.bot_mode!r}")
    await prepare()
    bot = Bot(token)
    try:
        me = await bot.get_me()
        common.bot_username = me.username or ""
        logger.info("HARI is running as @{} ({} mode)", common.bot_username, mode)
        dp = build_dispatcher()
        await (run_webhook(bot, dp) if mode == "webhook" else run_polling(bot, dp))
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
