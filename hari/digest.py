"""The owner's daily digest and the weekly report (spec 2.0 §20, §28; T28).

Daily, at OWNER_DIGEST_TIME on a work day: at most five matters that need the owner's decision or
attention, each with the fact, its effect, who owns it and by when — and nothing else. Ordinary
reminders never reach the owner. A matter is listed once; what did not fit moves to the next day.
An owner never receives a line about a matter they are the subject of. No complaint text, no chat.

Weekly, Friday at WEEKLY_REPORT_TIME, to HR and the owner: counts for the week. A ratio with nothing
under the line is "N/A", data HARI does not have is "номаълум" — never 0 or 100 (§16). A group
smaller than MIN_GROUP_SIZE is not broken out, so a number cannot point at one person.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import budget
from hari.config import settings
from hari.db.models import (
    AnswerLog, Application, DigestItem, Employee, HRCase, IntegrationStatus, KnowledgeDocument, OnboardingStep,
    OutboundMessage, ResponseItem, as_utc,
)
from hari.outbox import queue
from hari.worktime import local, schedule_for, today_local

MAX_ITEMS = 5
NA = "N/A"
UNKNOWN = "номаълум"


async def add(session: AsyncSession, key: str, kind: str, fact: str, *, impact: str = "", responsible: str = "",
              due: str = "", priority: int = 50, about_employee_id: str | None = None,
              about_role: str | None = None) -> bool:
    """Put a matter on the owner's list. The same key twice lists it once."""
    if (await session.execute(select(DigestItem.id).where(DigestItem.key == key[:120]))).first():
        return False
    session.add(DigestItem(key=key[:120], kind=kind, priority=priority, fact=fact, impact=impact,
                           responsible=responsible[:200], due=due[:100], about_employee_id=about_employee_id,
                           about_role=about_role))
    await session.flush()
    return True


def _hhmm(value: str, default: tuple[int, int]) -> tuple[int, int]:
    hour, _, minute = value.partition(":")
    if hour.strip().isdigit() and minute.strip().isdigit() and int(hour) < 24 and int(minute) < 60:
        return int(hour), int(minute)
    return default


def _reached(now: datetime, hhmm: tuple[int, int]) -> bool:
    loc = local(now)
    return (loc.hour, loc.minute) >= hhmm


async def _owners(session: AsyncSession) -> list[Employee]:
    return list((await session.execute(select(Employee).where(
        Employee.role == "owner", Employee.status == "active", Employee.telegram_user_id.is_not(None),
    ).order_by(Employee.code))).scalars())


# --------------------------------------------------------------------------- system state

async def system_status(session: AsyncSession, now: datetime) -> list[str]:
    """Only what is wrong or undecided. An empty list means nothing to report."""
    today = today_local(now)
    lines: list[str] = []
    missing = settings.missing_config()
    if missing:
        effect = "тегишли иш тўхтатилган" if settings.is_live else "синов режимида ишлаяпти"
        lines.append(f"Киритилмаган созламалар ({effect}): {', '.join(missing)}")
    spend = budget.state.status(now)
    if spend["limit_hit_today"]:
        lines.append(f"AI кунлик лимити тугади ({spend['limit']} та сўров): жавоблар тасдиқланган парча билан берилмоқда")
    elif spend["circuit_open"]:
        lines.append("AI хизмати кетма-кет хато берди: сўровлар вақтинча тўхтатилган")
    for row in (await session.execute(
            select(IntegrationStatus).where(IntegrationStatus.healthy.is_(False)))).scalars():
        lines.append(f"«{row.name}» манбаси ишламаяпти: ундан келадиган маълумот номаълум")
    counts = dict((await session.execute(
        select(OutboundMessage.status, func.count()).where(OutboundMessage.local_date == today)
        .group_by(OutboundMessage.status))).all())
    if counts.get("unknown"):
        lines.append(f"Етгани номаълум хабарлар: {counts['unknown']} (қайта юборилмади, IT текширади)")
    if counts.get("failed"):
        lines.append(f"Юборилмаган хабарлар: {counts['failed']}")
    docs = (await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.status == "approved"))).scalars().all()
    stale = [d.doc_id for d in docs if d.review_at and d.review_at < today]
    samples = [d for d in docs if is_placeholder(d.approved_by)]
    if stale:
        lines.append(f"Қайта кўриб чиқиш муддати ўтган ҳужжатлар: {len(stale)} ({', '.join(stale[:5])})")
    if samples:
        lines.append(f"Намуна (ҳали инсон тасдиқламаган) ҳужжатлар: {len(samples)}")
    return lines


def is_placeholder(approved_by: str | None) -> bool:
    """Nobody real approved it: empty, or the sample marker (spec 2.0 §7 "Placeholder … active бўла олмайди")."""
    value = (approved_by or "").strip().lower()
    return not value or value.startswith(("намуна", "namuna", "placeholder", "tbd", "todo", "null"))


# --------------------------------------------------------------------------- daily digest

def render(day, items: list[DigestItem], status: list[str]) -> str:  # noqa: ANN001
    lines = [f"HARI кунлик дайжест — {day:%d.%m.%Y}", f"Қарор ёки эътибор талаб қиладиган ишлар: {len(items)}"]
    for n, item in enumerate(items, start=1):
        lines.append(f"\n{n}. {item.fact}")
        if item.impact:
            lines.append(f"   Таъсир: {item.impact}")
        if item.responsible:
            lines.append(f"   Масъул: {item.responsible}")
        if item.due:
            lines.append(f"   Муддат: {item.due}")
    if status:
        lines.append("\nТизим ҳолати:")
        lines += [f"• {s}" for s in status]
    return "\n".join(lines)


def _visible(item: DigestItem, owner: Employee) -> bool:
    return item.about_employee_id != owner.id and item.about_role != "owner"


async def daily(session: AsyncSession, now: datetime) -> list[str]:
    """Queue today's digest for each owner, once, after the digest time. Nothing to decide → nothing sent."""
    if not _reached(now, _hhmm(settings.owner_digest_time, (17, 30))):
        return []
    today = today_local(now)
    await _add_waiting_applications(session, now)
    pending = (await session.execute(
        select(DigestItem).where(DigestItem.sent_on.is_(None)).order_by(DigestItem.priority, DigestItem.created_at)
    )).scalars().all()
    if not pending:
        return []
    status = await system_status(session, now)
    queued, shown = [], set()
    for owner in await _owners(session):
        if not schedule_for(owner).windows(today):
            continue  # a day off or a holiday in their calendar: tomorrow (§20)
        mine = [i for i in pending if _visible(i, owner)][:MAX_ITEMS]
        if not mine:
            continue
        msg = await queue(session, owner, "digest", f"digest:{owner.id}:{today}", render(today, mine, status), now)
        if msg:
            queued.append(msg.id)
            shown |= {i.id for i in mine}
    for item in pending:
        if item.id in shown:
            item.sent_on = today
    return queued


async def _add_waiting_applications(session: AsyncSession, now: datetime) -> None:
    """Applications nobody decided on within the promised days: one line a day, a count, no names."""
    limit = now - timedelta(days=settings.apply_reply_days + 2)  # work days promised; a weekend may fall inside
    rows = (await session.execute(select(Application).where(Application.status == "submitted"))).scalars().all()
    late = [a for a in rows if a.submitted_at and as_utc(a.submitted_at) < limit]
    if late:
        await add(session, f"apps_waiting:{today_local(now)}", "hiring", priority=60,
                  fact=f"{len(late)} та ариза {settings.apply_reply_days} иш кунидан ортиқ қарор кутмоқда.",
                  impact="Номзодларга ваъда қилинган жавоб муддати ўтди.", responsible="HR ва эга",
                  due="бугун")


# --------------------------------------------------------------------------- weekly report

def ratio(part: int | None, whole: int | None) -> str:
    """A share as a percentage. Nothing under the line → N/A; no data → номаълум. Never a made-up 0 or 100."""
    if part is None or whole is None:
        return UNKNOWN
    if whole == 0:
        return NA
    return f"{round(100 * part / whole)}% ({part}/{whole})"


def by_group(counts: dict[str, int], sizes: dict[str, int], minimum: int | None = None) -> list[str]:
    """Counts per group, with every group smaller than the minimum folded into one unnamed line (T28)."""
    minimum = settings.min_group_size if minimum is None else minimum
    lines, hidden = [], 0
    for group in sorted(counts):
        if sizes.get(group, 0) >= minimum:
            lines.append(f"{group}: {counts[group]}")
        else:
            hidden += counts[group]
    if hidden:
        lines.append(f"кичик гуруҳлар ({minimum} кишидан кам, алоҳида кўрсатилмайди): {hidden}")
    return lines


async def weekly_text(session: AsyncSession, now: datetime) -> str:
    since = now - timedelta(days=7)
    today = today_local(now)

    cases = (await session.execute(select(HRCase).where(HRCase.created_at >= since))).scalars().all()
    due = [c for c in cases if as_utc(c.first_response_due) <= now]
    on_time = [c for c in due if c.first_response_at and as_utc(c.first_response_at) <= as_utc(c.first_response_due)]
    closed = sum(1 for c in cases if c.status == "closed")
    staff = (await session.execute(select(Employee).where(Employee.status.in_(("active", "absent", "exiting"))))).scalars().all()
    sizes: dict[str, int] = {}
    for e in staff:
        sizes[e.department] = sizes.get(e.department, 0) + 1
    by_id = {e.id: e for e in staff}
    per_dept: dict[str, int] = {}
    for c in cases:
        if c.privacy == "sensitive":
            continue  # sensitive matters are counted in the total only
        dept = by_id[c.requester_id].department if c.requester_id in by_id else "—"
        per_dept[dept] = per_dept.get(dept, 0) + 1

    steps = (await session.execute(select(OnboardingStep).where(OnboardingStep.status == "submitted"))).scalars().all()
    accepted = (await session.execute(select(func.count()).select_from(OnboardingStep).where(
        OnboardingStep.status == "accepted", OnboardingStep.accepted_at >= since))).scalar_one()

    apps = (await session.execute(select(Application).where(Application.submitted_at >= since))).scalars().all()
    decided = (await session.execute(select(Application).where(Application.decided_at >= since))).scalars().all()
    hired = [a for a in decided if a.status == "hired" and a.submitted_at and a.decided_at]
    days_to_hire = (f"{round(sum((as_utc(a.decided_at) - as_utc(a.submitted_at)).days for a in hired) / len(hired))} кун"
                    if hired else NA)

    logs = (await session.execute(select(AnswerLog).where(AnswerLog.at >= since))).scalars().all()
    answered = sum(1 for g in logs if g.outcome == "answered")
    asked = sum(1 for g in logs if g.outcome in ("answered", "no_source", "fallback", "conflict"))
    votes = [g.was_helpful for g in logs if g.was_helpful is not None]

    waits = (await session.execute(select(ResponseItem).where(ResponseItem.created_at >= since))).scalars().all()

    lines = [
        f"HARI ҳафталик ҳисобот — {today:%d.%m.%Y} гача 7 кун",
        "",
        f"Мурожаатлар: {len(cases)} та очилди, {closed} таси ёпилди",
        f"Биринчи жавоб муддатида: {ratio(len(on_time), len(due))}",
    ]
    lines += [f"  {line}" for line in by_group(per_dept, sizes)]
    lines += [
        "",
        f"Мослашув: {accepted} қадам қабул қилинди, {len(steps)} қадам қабул кутмоқда",
        "",
        f"Ишга қабул: {len(apps)} ариза келди; суҳбатга {sum(1 for a in decided if a.status == 'interview')}, "
        f"рад {sum(1 for a in decided if a.status == 'rejected')}, ишга олинди {len(hired)}",
        f"Аризадан ишга олишгача: {days_to_hire}",
        "",
        f"HARI жавоблари: манбали жавоб {ratio(answered, asked)}; «тушунарли» баҳоси "
        f"{ratio(sum(1 for v in votes if v), len(votes))}",
        "",
        f"Жавоб кутиш: {len(waits)} иш берилди; эслатма {sum(1 for w in waits if w.reminder_count)}, "
        f"раҳбарга {sum(1 for w in waits if w.manager_notified_at)}, эга дайжестига "
        f"{sum(1 for w in waits if w.digest_at)}",
        "",
        f"Кетиш кўрсаткичи: {UNKNOWN} (расмий чиқиш реестри HARIга уланмаган)",
    ]
    status = await system_status(session, now)
    if status:
        lines.append("\nТизим ҳолати:")
        lines += [f"• {s}" for s in status]
    lines.append("\nБу рақамлар ҳеч кимнинг шахсий баҳоси эмас. Сабабларни HR ва раҳбар муҳокама қилади.")
    return "\n".join(lines)


async def weekly(session: AsyncSession, now: datetime) -> list[str]:
    loc = local(now)
    if loc.weekday() != 4 or not _reached(now, _hhmm(settings.weekly_report_time, (16, 0))):
        return []
    people = (await session.execute(select(Employee).where(
        Employee.role.in_(("owner", "hr")), Employee.status == "active", Employee.telegram_user_id.is_not(None),
    ).order_by(Employee.code))).scalars().all()
    if not people:
        return []
    key_day = today_local(now)
    already = (await session.execute(select(OutboundMessage.id).where(
        OutboundMessage.kind == "weekly", OutboundMessage.local_date == key_day))).first()
    if already:
        return []
    text = await weekly_text(session, now)
    queued = []
    for person in people:
        msg = await queue(session, person, "weekly", f"weekly:{person.id}:{key_day}", text, now)
        if msg:
            queued.append(msg.id)
    return queued
