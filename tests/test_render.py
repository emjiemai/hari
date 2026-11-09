"""Render hosting: database address forms, webhook address and secret, the web server (health + webhook)."""

from __future__ import annotations

import asyncio
import re

from aiohttp.test_utils import TestClient, TestServer
from pydantic import SecretStr

from hari.config import WEBHOOK_PATH, Settings, normalize_database_url, settings
from hari.main import build_dispatcher, build_web_app
from tests.test_bot_flow import Harness


def test_render_database_addresses_become_async_driver_addresses():
    assert normalize_database_url("postgres://u:p@dpg-x/hari") == ("postgresql+asyncpg://u:p@dpg-x/hari", {})
    assert normalize_database_url("postgresql://u:p@h.frankfurt-postgres.render.com/hari?sslmode=require") == (
        "postgresql+asyncpg://u:p@h.frankfurt-postgres.render.com/hari", {"ssl": "require"})
    assert normalize_database_url("postgresql://u:p@h/hari?sslmode=disable&application_name=hari") == (
        "postgresql+asyncpg://u:p@h/hari?application_name=hari", {})
    assert normalize_database_url("sqlite+aiosqlite:///./hari.db") == ("sqlite+aiosqlite:///./hari.db", {})


def test_webhook_address_and_secret():
    s = Settings(_env_file=None, render_external_url="https://hari.onrender.com/",
                 telegram_bot_token=SecretStr("1:abc"), webhook_secret=SecretStr("x+y/z=="))
    assert s.webhook_url == "https://hari.onrender.com" + WEBHOOK_PATH
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,256}", s.telegram_secret_token)  # Telegram's allowed characters
    assert "x+y/z==" not in s.telegram_secret_token
    explicit = Settings(_env_file=None, webhook_base_url="https://my.host", render_external_url="https://other")
    assert explicit.webhook_url == "https://my.host" + WEBHOOK_PATH
    assert Settings(_env_file=None).webhook_url == ""


async def test_web_server_health_and_webhook_secret(session_scope, staff):
    h = Harness()
    client = TestClient(TestServer(build_web_app(h.bot, build_dispatcher())))
    await client.start_server()
    try:
        resp = await client.get("/health")
        assert resp.status == 200 and (await resp.json()) == {"status": "ok"}

        update = {"update_id": 777001, "message": {
            "message_id": 1, "date": 1762500000, "chat": {"id": 4242, "type": "private"},
            "from": {"id": 4242, "is_bot": False, "first_name": "x"}, "text": "/start"}}
        wrong = await client.post(WEBHOOK_PATH, json=update,
                                  headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"})
        assert wrong.status == 401
        missing = await client.post(WEBHOOK_PATH, json=update)
        assert missing.status == 401
        await asyncio.sleep(0.1)
        assert h.to(4242) == []  # nothing handled without the secret

        ok = await client.post(WEBHOOK_PATH, json=update,
                               headers={"X-Telegram-Bot-Api-Secret-Token": settings.telegram_secret_token})
        assert ok.status == 200
        for _ in range(50):  # handled in the background after the 200
            if h.to(4242):
                break
            await asyncio.sleep(0.05)
        assert "боғланмаган" in h.to(4242)[0]
    finally:
        await client.close()
