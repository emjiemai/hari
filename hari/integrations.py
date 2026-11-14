"""Health of outside sources (spec 2.0 §9 "Маълумот сифатини текшириш"; T15).

When a source fails, what it would have told us is unknown — not "absent", not "did not answer".
The first failure opens one IT signal (the same failure repeating opens no second one); the first
success afterwards clears it. Nothing here connects to any outside system: a connector reports
its own result by calling `report_ok` / `report_failure`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit, digest
from hari.db.models import IntegrationStatus
from hari.worktime import fmt, now_utc, today_local

DATA_UNKNOWN = "DATA_UNKNOWN"
OK = "OK"


async def _row(session: AsyncSession, name: str) -> IntegrationStatus:
    row = await session.get(IntegrationStatus, name)
    if row is None:
        row = IntegrationStatus(name=name, healthy=True)
        session.add(row)
        await session.flush()
    return row


async def report_ok(session: AsyncSession, name: str, now: datetime | None = None) -> None:
    row = await _row(session, name)
    was_down = not row.healthy
    row.healthy, row.last_ok_at = True, now or now_utc()
    if was_down:
        await audit.record(session, "integration.status", "recovered", object_type="integration", object_id=name)


async def report_failure(session: AsyncSession, name: str, error: str, now: datetime | None = None) -> bool:
    """Returns True when this failure is new (the source was healthy): the moment to tell IT."""
    now = now or now_utc()
    row = await _row(session, name)
    newly_down = row.healthy
    row.healthy, row.last_error_at, row.last_error = False, now, error[:200]
    if newly_down:
        await audit.record(session, "integration.status", "failed", object_type="integration", object_id=name,
                           detail=error)
        await digest.add(
            session, f"it:{name}:{today_local(now)}", "it_incident", priority=30,
            fact=f"«{name}» манбаси ишламаяпти ({fmt(now)} дан). Ундан келадиган маълумот ҳозир номаълум.",
            impact="Шу манбага таянган хулосалар тўхтатилди: ҳеч ким «келмади» ёки «жавоб бермади» деб баҳоланмайди.",
            responsible="IT админ", due="бугун",
        )
    return newly_down


async def status(session: AsyncSession, name: str) -> str:
    """OK, or DATA_UNKNOWN while the source is down. A source that never reported is unknown too."""
    row = await session.get(IntegrationStatus, name)
    return OK if row is not None and row.healthy and row.last_ok_at is not None else DATA_UNKNOWN


async def is_down(session: AsyncSession, name: str) -> bool:
    row = await session.get(IntegrationStatus, name)
    return row is not None and not row.healthy


async def down_now(session: AsyncSession) -> list[IntegrationStatus]:
    return list((await session.execute(
        select(IntegrationStatus).where(IntegrationStatus.healthy.is_(False)).order_by(IntegrationStatus.name)
    )).scalars())
