"""Server-side timers: they keep running when nobody is online (spec §1 "HR мувофиқлаштирувчи";
spec 2.0 §20 "Автоном ишлаш режими").

Every proactive message is first written to `outbound_messages` with an idempotency key, then
sent, then marked. A restart in between resends a pending one instead of losing it, and the key
stops a second copy (A09). Ordinary employees get at most PROACTIVE_DAILY_LIMIT planned messages
a day, only on their work days and hours (spec §10, A07).

What a tick does (every minute, all times Asia/Tashkent):
- newcomers whose first day came become active; the morning onboarding plan goes out;
- the no-reply rule (hari/nonresponse.py): one reminder at 120 work minutes, the manager at 240,
  the owner's digest at 480;
- the owner's digest once a day after OWNER_DIGEST_TIME, the weekly report on Friday;
- IT is told once when an outside source or the AI budget stops;
- retention (only in live mode with an approved policy).
HARI_PAUSED=true stops all of it (the kill switch, T23).

A send that timed out may or may not have arrived. It is marked "unknown" and is NOT sent again
(T21); Telegram has no way to ask afterwards, so such messages are listed for IT in the system state.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter, TelegramServerError,
)
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import budget, digest, integrations, nonresponse, retention
from hari import onboarding as O
from hari.bot import texts as T
from hari.config import settings
from hari.db.models import Application, Employee, OnboardingPlan, OutboundMessage, Vacancy, as_utc
from hari.db.session import session_scope
from hari.outbox import PROACTIVE_KINDS, proactive_sent_today, queue
from hari.worktime import add_work_hours, is_work_time, local, now_utc, today_local

__all__ = ["PROACTIVE_KINDS", "proactive_sent_today", "queue", "deliver", "tick", "run_forever"]

TICK_SECONDS = 60
SENT, RETRY, UNKNOWN, FAILED = "sent", "retry", "unknown", "failed"


def send_outcome(exc: TelegramAPIError) -> str:
    """What a failed send tells us about whether the message may have arrived."""
    if isinstance(exc, (TelegramRetryAfter, TelegramServerError)):
        return RETRY  # Telegram refused it for now: it was not delivered
    if isinstance(exc, TelegramNetworkError):
        text = str(exc)
        # Could not even connect → certainly not sent. Anything else (a timeout, a dropped connection)
        # may have reached Telegram: the outcome is unknown.
        return RETRY if ("ClientConnector" in text or "Cannot connect" in text) else UNKNOWN
    if isinstance(exc, TelegramForbiddenError):
        return FAILED  # the person blocked the bot: trying again changes nothing
    return RETRY


async def deliver(bot: Bot, msg_id: str) -> bool:
    async with session_scope() as session:
        msg = await session.get(OutboundMessage, msg_id)
        if msg is None or msg.status != "pending":
            return False
        emp = await session.get(Employee, msg.employee_id)
        msg.attempts += 1
        text = msg.text
    outcome, error = FAILED, ""
    if emp is not None and emp.telegram_user_id:
        try:
            await bot.send_message(emp.telegram_user_id, T.say(emp, text))
            outcome = SENT
        except TelegramAPIError as exc:
            outcome, error = send_outcome(exc), type(exc).__name__
            logger.warning("Proactive message to {} not confirmed: {} ({})", emp.code, error, outcome)
    async with session_scope() as session:
        msg = await session.get(OutboundMessage, msg_id)
        now = now_utc()
        if outcome == SENT:
            msg.status, msg.sent_at = "sent", now
            await integrations.report_ok(session, nonresponse.DELIVERY_CHANNEL, now)
            if msg.idempotency_key.startswith("redeliver:"):
                await nonresponse.confirm_delivery(session, msg.idempotency_key.split(":", 1)[1], now)
        elif outcome == UNKNOWN:
            msg.status = "unknown"  # never resent blindly; listed for IT (T21)
            await integrations.report_failure(session, nonresponse.DELIVERY_CHANNEL, f"send {error}", now)
        elif outcome == FAILED or msg.attempts >= settings.max_retries:
            msg.status = "failed"
    return outcome == SENT


def nudge_text(emp: Employee, steps: list) -> str:  # noqa: ANN001
    lines = [f"Хайрли тонг, {emp.full_name.split()[1] if len(emp.full_name.split()) > 1 else emp.full_name}. "
             "Мослашув режангиздаги очиқ қадамлар:"]
    lines += [f"• {s.title}" for s in steps[:5]]
    lines.append("\n«Режам» тугмасини босинг. Қайси қадам тушунарсиз бўлса, ёзинг.")
    return "\n".join(lines)


async def onboarding_nudges(session: AsyncSession, now: datetime) -> list[str]:
    queued: list[str] = []
    plans = (await session.execute(select(OnboardingPlan).where(OnboardingPlan.status == "active"))).scalars().all()
    for plan in plans:
        emp = await session.get(Employee, plan.employee_id)
        if emp is None or emp.status != "active" or not emp.telegram_user_id or not is_work_time(emp, now):
            continue  # on leave/absent, not bound yet, or outside their hours: wait (A07)
        await session.refresh(plan, ["steps"])
        steps = O.due_open_steps(emp, plan, today_local(now))
        if not steps:
            continue
        msg = await queue(session, emp, "onboarding", f"onb:{emp.id}:{today_local(now)}", nudge_text(emp, steps), now)
        if msg:
            queued.append(msg.id)
    return queued


async def system_alerts(session: AsyncSession, now: datetime) -> list[str]:
    """Tell IT (without one, the owner) once when a source stops or the AI budget runs out (spec 2.0 §23)."""
    today = today_local(now)
    notes: list[tuple[str, str]] = []
    for row in await integrations.down_now(session):
        notes.append((f"it:{row.name}:{today}",
                      f"⚠️ «{row.name}» манбаси ишламаяпти. Ундан келадиган маълумот ҳозир номаълум деб "
                      "ҳисобланади; ҳеч ким шу сабабли баҳоланмайди. Текширинг."))
    spend = budget.state.status(now)
    if spend["limit_hit_today"]:
        await digest.add(
            session, f"llm_limit:{today}", "budget", priority=30,
            fact=f"AI сўровларининг кунлик лимити тугади ({spend['limit']} та).",
            impact="Эртагача HARI саволларга тасдиқланган ҳужжат парчаси билан жавоб беради, AI матнисиз.",
            responsible="IT админ ва эга", due="лимитни кўриб чиқиш")
        notes.append((f"llm_limit:{today}", f"⚠️ AI кунлик лимити тугади ({spend['limit']} та сўров). "
                                              "AI сўровлари эртагача тўхтатилди."))
    if not notes:
        return []
    people = (await session.execute(select(Employee).where(
        Employee.role == "it_admin", Employee.status == "active", Employee.telegram_user_id.is_not(None)))).scalars().all()
    if not people:
        people = (await session.execute(select(Employee).where(
            Employee.role == "owner", Employee.status == "active", Employee.telegram_user_id.is_not(None)))).scalars().all()
    queued = []
    for key, text in notes:
        for person in people:
            msg = await queue(session, person, "alert", f"{key}:{person.id}", text, now)
            if msg:
                queued.append(msg.id)
    return queued


async def activate_started(session: AsyncSession, now: datetime) -> None:
    rows = (await session.execute(select(Employee).where(Employee.status == "preparing"))).scalars().all()
    for emp in rows:
        if emp.identity_verified_at and emp.start_date and emp.start_date <= today_local(now):
            emp.status = "active"


async def pending_retries(session: AsyncSession, now: datetime) -> list[str]:
    rows = (await session.execute(select(OutboundMessage).where(OutboundMessage.status == "pending"))).scalars().all()
    return [m.id for m in rows if as_utc(m.created_at) < now - timedelta(minutes=2)
            and as_utc(m.created_at) > now - timedelta(hours=6)]


WAIT_WORK_HOURS = 9  # one work day on the default schedule (09:00–18:00)


async def applicants_waiting(session: AsyncSession, now: datetime) -> list[str]:
    """An application nobody has interviewed or rejected for one work day: remind every owner and HR,
    once a day, until someone decides. The applicant is not messaged here."""
    from hari.apply.report import recipients  # here: the report module imports the bot UI

    rows = (await session.execute(select(Application).where(
        Application.status == "submitted", Application.submitted_at.is_not(None)))).scalars().all()
    if not rows:
        return []
    people = await recipients(session)
    queued: list[str] = []
    for app in rows:
        if now < add_work_hours(None, as_utc(app.submitted_at), WAIT_WORK_HOURS):
            continue
        vac = await session.get(Vacancy, app.vacancy_id) if app.vacancy_id else None
        where = f" ({vac.title})" if vac else ""
        text = (f"Ҳей, {app.number} — {app.full_name or 'номзод'}{where} сизни кутяпти. "
                "Кўриб чиқиб, суҳбатга чақиринг ёки рад этинг.")
        for person in people:
            msg = await queue(session, person, "applicant_wait", f"app_wait:{app.id}:{person.id}:{today_local(now)}",
                              text, now)
            if msg:
                queued.append(msg.id)
    return queued


async def unreported(session: AsyncSession, now: datetime) -> list[str]:
    """Finished applications whose report never went out (a crash or deploy right after the last answer).
    Two minutes' grace: the applicant's own flow is still sending a fresh one."""
    rows = (await session.execute(select(Application).where(
        Application.status == "submitted", Application.reported_at.is_(None),
        Application.telegram_user_id.is_not(None)))).scalars().all()
    return [a.id for a in rows if a.submitted_at and as_utc(a.submitted_at) < now - timedelta(minutes=2)]


async def tick(bot: Bot, now: datetime | None = None) -> int:
    if settings.hari_paused:
        return 0  # the kill switch: no proactive message and no automatic change (T23)
    now = now or now_utc()
    async with session_scope() as session:
        await activate_started(session, now)
        ids = await pending_retries(session, now)
        ids += await onboarding_nudges(session, now)
        ids += await applicants_waiting(session, now)
        ids += await nonresponse.sweep(session, now)
        ids += await system_alerts(session, now)
        ids += await digest.daily(session, now)
        ids += await digest.weekly(session, now)
        purged = await retention.run(session, now)
        reports = await unreported(session, now)
    if purged:
        logger.info("Retention: personal data removed from {} rejected applications", purged)
    sent = 0
    for msg_id in dict.fromkeys(ids):
        sent += await deliver(bot, msg_id)
    from hari.apply.report import evaluate_and_report  # here: the report module imports the bot UI

    for app_id in reports:
        sent += bool(await evaluate_and_report(bot, app_id))
    return sent


async def run_forever(bot: Bot) -> None:
    logger.info("Scheduler started (every {} s, Tashkent now {})", TICK_SECONDS, local(now_utc()).strftime("%H:%M"))
    while True:
        try:
            await tick(bot)
        except Exception as exc:  # noqa: BLE001 — one bad tick must not stop the timers
            logger.exception("Scheduler tick failed: {}", exc)
        await asyncio.sleep(TICK_SECONDS)
