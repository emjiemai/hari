"""When is someone "not answering"? (spec 2.0 §19, IT 008; T12–T15, T22)

Only for a verified employee, on work that needs their answer, that was really delivered, during
their confirmed shift. "Online" or "read" marks are not used at all. One missing reply is never a
conclusion about the person: it leads to one private reminder, then to their manager, then to one
minimal line in the owner's digest — and to nothing else.

`decide` is the spec's algorithm, with no database and no clock of its own. `sweep` feeds it the
facts for every open item and carries out what it returns.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit, digest, integrations
from hari.config import settings
from hari.db.models import Employee, HRCase, OnboardingStep, OutboundMessage, ResponseItem, as_utc
from hari.outbox import queue
from hari.worktime import business_minutes_between, is_business_time, now_utc, schedule_for

NOT_ELIGIBLE = "NOT_ELIGIBLE"
DELIVERY_UNKNOWN = "DELIVERY_UNKNOWN"
DATA_UNKNOWN = "DATA_UNKNOWN"
PAUSED = "PAUSED"
RESOLVED = "RESOLVED"
WAITING = "WAITING"

DELIVERY_CHANNEL = "telegram"


@dataclass(frozen=True)
class Facts:
    identity_known: bool
    response_required: bool
    delivery_confirmed: bool
    schedule_known: bool
    integrations_healthy: bool
    on_leave: bool  # leave, approved absence or any state other than "working"
    shift_open: bool
    elapsed_minutes: int  # business minutes since delivery (or since the agreed "I'll answer at …")
    replied: bool  # a real reply or an accepted update of the work
    reminder_count: int
    owner_critical: bool = False


@dataclass(frozen=True)
class Decision:
    result: str
    remind: bool = False  # one private reminder
    manager: bool = False  # open or update the matter with the manager
    owner_digest: bool = False  # one minimal line in the owner's digest


def decide(facts: Facts, thresholds: tuple[int, int, int] | None = None) -> Decision:
    """Spec 2.0 §19 "Детерминистик алгоритм", line for line."""
    first, second, third = thresholds or settings.nonresponse_thresholds
    if not facts.identity_known or not facts.response_required:
        return Decision(NOT_ELIGIBLE)
    if not facts.delivery_confirmed:
        return Decision(DELIVERY_UNKNOWN)
    if not facts.schedule_known or not facts.integrations_healthy:
        return Decision(DATA_UNKNOWN)
    if facts.on_leave or not facts.shift_open:
        return Decision(PAUSED)
    if facts.replied:
        return Decision(RESOLVED)
    return Decision(
        WAITING,
        remind=facts.elapsed_minutes >= first and facts.reminder_count == 0,
        manager=facts.elapsed_minutes >= second,
        owner_digest=facts.elapsed_minutes >= third or facts.owner_critical,
    )


# --------------------------------------------------------------------------- records

def case_title(case: HRCase) -> str:
    """What a manager may see about someone else's waiting case: a sensitive one shows its number only."""
    return f"{case.number} ({case.title})" if case.privacy != "sensitive" else f"{case.number} (нозик мурожаат)"


async def track(session: AsyncSession, employee: Employee, kind: str, ref_id: str, title: str, delivered: bool,
                now: datetime | None = None, about_employee_id: str | None = None) -> ResponseItem:
    """HARI handed this person something that needs their answer. `delivered` is Telegram's own confirmation."""
    now = now or now_utc()
    item = (await session.execute(select(ResponseItem).where(
        ResponseItem.kind == kind, ResponseItem.ref_id == ref_id, ResponseItem.employee_id == employee.id,
    ))).scalar_one_or_none()
    if item is None:
        item = ResponseItem(employee_id=employee.id, kind=kind, ref_id=ref_id, title=title[:300],
                            about_employee_id=about_employee_id, created_at=now)
        session.add(item)
    elif item.state != "open":  # the same work came back (a step returned and sent again): a new wait
        item.state, item.resolved_at, item.reminder_count = "open", None, 0
        item.manager_notified_at = item.digest_at = item.agreed_until = None
        item.delivery_status, item.delivered_at, item.delivery_attempts = "unknown", None, 0
    if delivered and item.delivery_status != "confirmed":
        item.delivery_status, item.delivered_at = "confirmed", now
    await session.flush()
    return item


async def resolve(session: AsyncSession, kind: str, ref_id: str, now: datetime | None = None) -> int:
    """The work was answered (by anyone allowed to): every wait on it ends."""
    rows = (await session.execute(select(ResponseItem).where(
        ResponseItem.kind == kind, ResponseItem.ref_id == ref_id, ResponseItem.state == "open",
    ))).scalars().all()
    for item in rows:
        item.state, item.resolved_at, item.last_result = "resolved", now or now_utc(), RESOLVED
    return len(rows)


async def confirm_delivery(session: AsyncSession, item_id: str, now: datetime) -> None:
    item = await session.get(ResponseItem, item_id)
    if item is not None and item.state == "open" and item.delivery_status != "confirmed":
        item.delivery_status, item.delivered_at = "confirmed", now


async def _still_waiting(session: AsyncSession, item: ResponseItem) -> bool:
    """Is the underlying work still unanswered by this person?"""
    if item.kind == "case":
        case = await session.get(HRCase, item.ref_id)
        return case is not None and case.status == "submitted" and case.assignee_id == item.employee_id
    if item.kind == "step":
        step = await session.get(OnboardingStep, item.ref_id)
        return step is not None and step.status == "submitted"
    return True


async def _backfill(session: AsyncSession, now: datetime) -> None:
    """Cases handed out before this rule existed, or whose card never went through, get an item.
    Their delivery is unknown, so the first thing that happens is one more delivery attempt."""
    cases = (await session.execute(
        select(HRCase).where(HRCase.status == "submitted", HRCase.assignee_id.is_not(None))
    )).scalars().all()
    for case in cases:
        exists = (await session.execute(select(ResponseItem.id).where(
            ResponseItem.kind == "case", ResponseItem.ref_id == case.id, ResponseItem.employee_id == case.assignee_id,
        ))).first()
        if exists is None:
            assignee = await session.get(Employee, case.assignee_id)
            if assignee is not None:
                await track(session, assignee, "case", case.id, case_title(case), delivered=False, now=now,
                            about_employee_id=case.about_employee_id)


async def _escalation_target(session: AsyncSession, emp: Employee, item: ResponseItem) -> Employee | None:
    """Their manager; without one, HR, then the owner. Never the person the matter is about."""
    skip = {emp.id, item.about_employee_id}

    def usable(person: Employee | None) -> bool:
        return (person is not None and person.id not in skip and person.status == "active"
                and person.telegram_user_id is not None)

    manager = await session.get(Employee, emp.manager_id) if emp.manager_id else None
    if usable(manager):
        return manager
    for role in ("hr", "owner"):
        rows = (await session.execute(
            select(Employee).where(Employee.role == role, Employee.status == "active").order_by(Employee.code)
        )).scalars().all()
        person = next((p for p in rows if usable(p)), None)
        if person is not None:
            return person
    return None


def _hours(minutes: int) -> str:
    return f"{minutes // 60} иш соати" if minutes % 60 == 0 else f"{minutes} иш дақиқаси"


async def sweep(session: AsyncSession, now: datetime) -> list[str]:
    """Apply the rule to every open item. Returns the ids of the outbox messages to send."""
    await _backfill(session, now)
    telegram_ok = not await integrations.is_down(session, DELIVERY_CHANNEL)
    thresholds = settings.nonresponse_thresholds
    queued: list[str] = []
    reminders: dict[str, tuple[Employee, list[ResponseItem]]] = {}

    items = (await session.execute(
        select(ResponseItem).where(ResponseItem.state == "open").order_by(ResponseItem.created_at)
    )).scalars().all()
    for item in items:
        if not await _still_waiting(session, item):
            item.state, item.resolved_at, item.last_result = "resolved", now, RESOLVED
            continue
        emp = await session.get(Employee, item.employee_id)
        schedule = schedule_for(emp)
        since = max(filter(None, (as_utc(item.delivered_at), as_utc(item.agreed_until))), default=None)
        elapsed = business_minutes_between(schedule, since, now) if since else 0
        decision = decide(Facts(
            identity_known=emp is not None and emp.identity_verified_at is not None
            and emp.telegram_user_id is not None,
            response_required=True,
            delivery_confirmed=item.delivery_status == "confirmed",
            schedule_known=bool(schedule.days) and schedule.end_hour > schedule.start_hour,
            integrations_healthy=telegram_ok,
            on_leave=emp is None or emp.status != "active",
            shift_open=is_business_time(schedule, now),
            elapsed_minutes=elapsed,
            replied=False,
            reminder_count=item.reminder_count,
        ), thresholds)
        item.last_result = decision.result

        if decision.result == DELIVERY_UNKNOWN and emp is not None and emp.status == "active" and telegram_ok \
                and is_business_time(schedule, now):
            queued += await _unconfirmed_delivery(session, emp, item, now)
            continue
        if decision.result != WAITING:
            continue  # NOT_ELIGIBLE, DATA_UNKNOWN, PAUSED: nothing is sent and nothing is concluded

        if decision.remind:
            reminders.setdefault(emp.id, (emp, []))[1].append(item)
        if decision.manager and item.manager_notified_at is None:
            target = await _escalation_target(session, emp, item)
            if target is not None:
                msg = await queue(
                    session, target, "escalation", f"nr_mgr:{item.id}",
                    f"⚠️ {item.title}: {emp.full_name} {_hours(thresholds[1])}дан бери жавоб бермади. "
                    "Бу ходим ҳақида хулоса эмас — тўсиқ, иш юки ва масъулни текширинг.", now)
                if msg:
                    queued.append(msg.id)
            item.manager_notified_at = now
            await audit.record(session, "nonresponse.manager", "queued" if target else "no_recipient",
                               object_type=item.kind, object_id=item.ref_id)
        if decision.owner_digest and item.digest_at is None:
            await digest.add(
                session, f"nr:{item.id}", "nonresponse", priority=40,
                fact=f"{item.title}: {_hours(thresholds[2])}дан бери жавобсиз.",
                impact="Мурожаат ёки қабул кутиб қолмоқда. Сабаб аниқланмаган: тўсиқ, иш юки ёки масъул нотўғри бўлиши мумкин.",
                responsible=emp.full_name, due="кейинги иш куни",
                about_employee_id=item.about_employee_id,
            )
            item.digest_at = now

    # One reminder per person for everything that became due together (the daily limit applies; what
    # does not fit waits for the next shift, T22).
    for emp, due in reminders.values():
        ids = ",".join(sorted(i.id for i in due))
        lines = ["Жавобингиз кутилмоқда:"] + [f"• {i.title}" for i in due[:8]]
        lines.append("\n«Иш навбати»ни очинг. Ҳозир жавоб бера олмасангиз, сабаби ва вақтини ёзиб қўйинг.")
        msg = await queue(session, emp, "reminder", "remind:" + hashlib.sha256(ids.encode()).hexdigest()[:40],
                          "\n".join(lines), now)
        if msg:
            queued.append(msg.id)
            for item in due:
                item.reminder_count += 1
    return queued


async def _unconfirmed_delivery(session: AsyncSession, emp: Employee, item: ResponseItem, now: datetime) -> list[str]:
    """Telegram never confirmed the card. Try once more; if that fails too, ask their manager to pass it on.
    No conclusion about the person is drawn either way (T14)."""
    key = f"redeliver:{item.id}"
    if item.delivery_attempts == 0:
        item.delivery_attempts = 1
        msg = await queue(session, emp, "redelivery", key,
                          f"{item.title} — сизнинг жавобингизни кутмоқда. «Иш навбати»ни очинг.", now)
        return [msg.id] if msg else []
    attempt = (await session.execute(
        select(OutboundMessage).where(OutboundMessage.idempotency_key == key)
    )).scalar_one_or_none()
    if attempt is not None and attempt.status == "pending":
        return []  # still being tried
    if item.manager_notified_at is None:
        item.manager_notified_at = now
        target = await _escalation_target(session, emp, item)
        await audit.record(session, "nonresponse.delivery", "unconfirmed", object_type=item.kind,
                           object_id=item.ref_id)
        if target is not None:
            msg = await queue(
                session, target, "escalation", f"nr_deliv:{item.id}",
                f"{emp.full_name}га HARI хабари етгани тасдиқланмади ({item.title}). "
                "Илтимос, уни ўзингиз хабардор қилинг. Бу жавобсизлик ҳисобланмайди.", now)
            return [msg.id] if msg else []
    return []
