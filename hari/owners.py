"""Owners and roles.

`HARI_OWNERS` (Render → Environment) names the Telegram accounts that are always owners, so the first
owner needs no server shell. After that, owners change anyone's role from the bot. Taking an ID out of
the setting never demotes anyone: demotion is a deliberate act in the bot.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit
from hari.db.models import ROLES, Employee
from hari.employees import CardInput, create_card
from hari.worktime import now_utc, today_local

PLACEHOLDER = "Эгаси"


class RoleError(ValueError):
    """Shown to the owner as is (Uzbek)."""


async def ensure_owners(session: AsyncSession, telegram_ids: list[int]) -> list[str]:
    touched: list[str] = []
    for tg in telegram_ids:
        emp = (await session.execute(
            select(Employee).where(Employee.telegram_user_id == tg)
        )).scalar_one_or_none()
        if emp is not None and emp.status == "archived":
            continue
        if emp is None:
            emp, _ = await create_card(session, CardInput(
                full_name=f"{PLACEHOLDER} {tg}", department="Раҳбарият", position="Эгаси", role="owner",
                start_date=today_local(),
            ), created_by=None)
            emp.telegram_user_id, emp.identity_verified_at, emp.status = tg, now_utc(), "active"
            touched.append(emp.code)
        elif emp.role != "owner":
            old, emp.role = emp.role, "owner"
            await audit.record(session, "role.change", "changed", object_type="employee", object_id=emp.id,
                               detail=f"{old}->owner by HARI_OWNERS")
            touched.append(emp.code)
    await session.flush()
    return touched


async def claim_owner_name(session: AsyncSession, telegram_user_id: int, first: str, last: str | None) -> None:
    """An owner card made from the setting carries a placeholder name until its owner first writes /start."""
    emp = (await session.execute(
        select(Employee).where(Employee.telegram_user_id == telegram_user_id)
    )).scalar_one_or_none()
    if emp is None or emp.full_name != f"{PLACEHOLDER} {telegram_user_id}":
        return
    name = " ".join(f"{first or ''} {last or ''}".split())
    emp.full_name = name if len(name.split()) >= 2 else f"{name or PLACEHOLDER} {PLACEHOLDER}"


async def change_role(session: AsyncSession, actor: Employee, target_id: str, role: str) -> Employee:
    if actor.role != "owner":
        raise RoleError("Ролни фақат эгаси ўзгартира олади.")
    if role not in ROLES:
        raise RoleError("Бундай роль йўқ.")
    target = await session.get(Employee, target_id)
    if target is None:
        raise RoleError("Ходим топилмади.")
    if target.role == "owner" and role != "owner":
        owners = (await session.execute(select(func.count()).select_from(Employee).where(
            Employee.role == "owner", Employee.status == "active"))).scalar_one()
        if owners <= 1:
            raise RoleError("Охирги эгасининг ролини ўзгартириб бўлмайди. Аввал бошқа эгаси тайинланг.")
    old, target.role = target.role, role
    await audit.record(session, "role.change", "changed", actor_id=actor.id, object_type="employee",
                       object_id=target.id, detail=f"{old}->{role}")
    return target
