"""Owners from the HARI_OWNERS setting, and who may change roles."""

from __future__ import annotations

import pytest

from hari.employees import by_telegram
from hari.owners import RoleError, change_role, claim_owner_name, ensure_owners


async def test_hari_owners_creates_or_promotes_and_never_demotes(session_scope, staff):
    async with session_scope() as s:
        emp = await staff(s, "Ходимов Акмал", tg=77)
        touched = await ensure_owners(s, [77, 8632661510])
        assert emp.role == "owner" and len(touched) == 2
        new = await by_telegram(s, 8632661510)
        assert new.role == "owner" and new.status == "active" and new.full_name == "Эгаси 8632661510"
        assert new.identity_verified_at is not None
        assert await ensure_owners(s, [77, 8632661510]) == []  # a second start changes nothing
        await claim_owner_name(s, 8632661510, "Шахзод", None)
        assert new.full_name == "Шахзод Эгаси"
        await claim_owner_name(s, 8632661510, "Бошқа", "Исм")  # only the placeholder is ever replaced
        assert new.full_name == "Шахзод Эгаси"
        await claim_owner_name(s, 77, "Янги", "Исм")
        assert emp.full_name == "Ходимов Акмал"


async def test_change_role_rules(session_scope, staff):
    async with session_scope() as s:
        boss = await staff(s, "Раҳбаров Бек", "owner", tg=1)
        hr = await staff(s, "Кадрова Зайнаб", "hr", tg=2)
        worker = await staff(s, "Ишчи Олим", tg=3)
        with pytest.raises(RoleError):
            await change_role(s, hr, worker.id, "manager")  # not an owner
        with pytest.raises(RoleError):
            await change_role(s, boss, boss.id, "hr")  # the last owner
        with pytest.raises(RoleError):
            await change_role(s, boss, worker.id, "king")
        assert (await change_role(s, boss, worker.id, "manager")).role == "manager"
        await change_role(s, boss, hr.id, "owner")
        assert (await change_role(s, boss, boss.id, "hr")).role == "hr"  # another owner exists now


async def test_placeholder_owner_gets_their_telegram_name_on_start(session_scope, monkeypatch):
    from hari import main
    from hari.config import settings
    from tests.test_bot_flow import Harness

    monkeypatch.setattr(settings, "hari_owners", "8632661510")
    monkeypatch.setattr(settings, "knowledge_dir", "knowledge/samples")
    await main.prepare()
    h = Harness()
    replies = await h.say(8632661510, "/start")
    assert "Хуш келибсиз, u8632661510 Эгаси" in replies[0]
    async with session_scope() as s:
        from hari.apply.vacancies import list_open
        assert len(await list_open(s)) == 7  # prepare() also loads the starting vacancies
