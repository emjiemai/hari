"""The application report for the boss and HR: HARI's view first, then the details, then the answers."""

from __future__ import annotations

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari.apply import evaluate as E
from hari.apply.evaluate import VERDICTS
from hari.apply.texts import BTN_HIRE, BTN_INTERVIEW, BTN_REJECT, LANG_NAMES, NO_AI, STATUS_LABELS
from hari.apply.vacancies import title_for
from hari.bot.ui import AppActCb, inline, notify, send_proof
from hari.db.models import Application, Employee, Vacancy
from hari.db.session import session_scope
from hari.worktime import now_utc

PART_LIMIT = 4000  # Telegram allows 4096 per message


def _site(source: str) -> str | None:
    if source == "manual":
        return "Қўлда қўшилган"
    if source.startswith("apply_") and len(source) > 6:
        return source[6:].replace("_", " ").capitalize()
    return None


def _blocks(app: Application, vacancy_title: str) -> list[str]:
    head = [f"Янги ариза {app.number or '—'} — {vacancy_title}",
            f"HARI баҳоси: {VERDICTS.get(app.verdict or '', NO_AI)}"]
    if app.reason:
        head.append(f"Сабаб: {app.reason}")
    details = []
    if app.logic:
        right, total = sum(1 for r in app.logic if r.get("correct")), len(app.logic)
        secs = ", ".join(f"{r.get('seconds')} с" for r in app.logic if r.get("seconds") is not None)
        details.append(f"Мантиқ: {right}/{total}" + (f" ({secs})" if secs else ""))
    details += [f"Исм: {app.full_name}", f"Телефон: {app.phone or '—'}"]
    if app.source != "manual":
        details.append(f"Тил: {LANG_NAMES.get(app.language, app.language)}")
    site = _site(app.source or "")
    if site:
        details.append(f"Сайт: {site}")
    if app.status != "submitted":
        details.append(f"Ҳолат: {STATUS_LABELS.get(app.status, app.status)}")
    if app.note:
        details.append(f"Изоҳ (HR): {app.note}")
    blocks = ["\n".join(head) + "\n\n" + "\n".join(details)]
    if app.experience:
        blocks.append(f"Тажриба: {app.experience}")
    notes = app.answer_notes or []
    for i, row in enumerate(app.job or []):
        lines = [f"{i + 1}-савол: {row.get('q', '')}", f"Жавоб: {row.get('a') or '—'}"]
        if i < len(notes):
            lines.append(f"Изоҳ: {notes[i]}")
        blocks.append("\n".join(lines))
    return blocks


def report_parts(app: Application, vacancy_title: str) -> list[str]:
    """Whole blocks packed into messages under Telegram's limit; the verdict always in the first one."""
    parts: list[str] = []
    for block in _blocks(app, vacancy_title):
        while len(block) > PART_LIMIT:  # never happens with clipped answers, but nothing may be lost
            parts.append(block[:PART_LIMIT])
            block = block[PART_LIMIT:]
        if parts and len(parts[-1]) + 2 + len(block) <= PART_LIMIT:
            parts[-1] += "\n\n" + block
        else:
            parts.append(block)
    return parts


def report_buttons(app: Application) -> InlineKeyboardMarkup | None:
    row = []
    if app.status == "submitted":
        row.append((BTN_INTERVIEW, AppActCb(action="interview", app_id=app.id)))
    if app.status in ("submitted", "interview"):
        row += [(BTN_REJECT, AppActCb(action="reject", app_id=app.id)),
                (BTN_HIRE, AppActCb(action="hire", app_id=app.id))]
    return inline([row]) if row else None


async def vacancy_title(session: AsyncSession, app: Application) -> str:
    vac = await session.get(Vacancy, app.vacancy_id) if app.vacancy_id else None
    return title_for(vac, "uz_cyrl")


async def recipients(session: AsyncSession) -> list[Employee]:
    return list((await session.execute(select(Employee).where(
        Employee.role.in_(("owner", "hr")), Employee.status == "active", Employee.telegram_user_id.is_not(None),
    ).order_by(Employee.code))).scalars())


async def send_to(bot: Bot, person: Employee, parts: list[str], buttons: InlineKeyboardMarkup | None,
                  resume_file_id: str | None) -> bool:
    ok = True
    for i, part in enumerate(parts):
        ok = await notify(bot, person, part, buttons if i == len(parts) - 1 else None) and ok
    if ok:
        await send_proof(bot, person, resume_file_id)
    return ok


async def send_report(bot: Bot, session: AsyncSession, app: Application) -> int:
    parts = report_parts(app, await vacancy_title(session, app))
    reached = 0
    for person in await recipients(session):
        reached += await send_to(bot, person, parts, report_buttons(app), app.resume_file_id)
    if reached:
        app.reported_at = now_utc()
    else:
        logger.warning("Application {}: nobody to report to (no active owner or HR with Telegram)", app.number)
    return reached


async def tell_applicant(bot: Bot, app: Application, text: str) -> bool:
    if not app.telegram_user_id:
        return False
    try:
        await bot.send_message(app.telegram_user_id, text)
        return True
    except TelegramAPIError as exc:
        logger.warning("Message to applicant {} not delivered: {}", app.number, type(exc).__name__)
        return False


async def evaluate_and_report(bot: Bot, app_id: str) -> int:
    """HARI's view (outside any session: the AI may take seconds), then the report to owners and HR.
    Used when an applicant finishes, and by the timer for a report that never went out."""
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        vac = await session.get(Vacancy, app.vacancy_id) if app.vacancy_id else None
        needs_view = app.verdict is None
    evaluation = await E.evaluate(app, vac) if needs_view else None
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        if evaluation is not None:
            app.verdict, app.reason = evaluation.verdict, evaluation.reason
            app.answer_notes, app.ai_model = evaluation.notes, evaluation.model
        return await send_report(bot, session, app)
