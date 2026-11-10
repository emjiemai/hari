"""Job applications without Telegram: the draft and its steps, input checks, the logic test, numbering
and decisions. The bot handlers only translate messages into these calls.

Progress lives in the `applications` row, so a redeploy in the middle of an application loses nothing.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit
from hari.apply.logic_bank import BANK, by_id
from hari.db.models import Application, ApplicationCounter, Employee, Vacancy, as_utc
from hari.worktime import local, now_utc

STEPS = ("lang", "consent", "name", "phone", "vacancy", "experience", "resume", "logic", "job", "done")
LOGIC_COUNT = 3
ANSWER_LIMIT = 1500
DECISIONS = {"interview": ("submitted",), "reject": ("submitted", "interview")}
HIRE_FROM = ("submitted", "interview")


class ApplyError(ValueError):
    """Codes: vacancy_closed, duplicate."""


async def draft_for(session: AsyncSession, tg: int) -> Application | None:
    return (await session.execute(
        select(Application).where(Application.telegram_user_id == tg, Application.status == "draft")
        .order_by(Application.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def last_submitted(session: AsyncSession, tg: int) -> Application | None:
    return (await session.execute(
        select(Application).where(Application.telegram_user_id == tg, Application.status != "draft")
        .order_by(Application.submitted_at.desc()).limit(1)
    )).scalar_one_or_none()


async def start_or_resume(session: AsyncSession, tg: int, source: str) -> tuple[Application, bool]:
    draft = await draft_for(session, tg)
    if draft is not None:
        return draft, False
    app = Application(telegram_user_id=tg, source=(source or "apply")[:40], step="lang", step_started_at=now_utc(),
                      logic=[], job=[], answer_notes=[])
    session.add(app)
    await session.flush()
    return app, True


def go(app: Application, step: str, now: datetime) -> None:
    if step not in STEPS:
        raise ValueError(step)
    app.step, app.step_started_at = step, now


def clean_name(text: str) -> str | None:
    words = (text or "").split()
    if len(words) < 2 or not all(re.search(r"[^\W\d_]", w) for w in words):
        return None
    name = " ".join(words)
    return name if len(name) <= 200 else None


def clean_phone(text: str) -> str | None:
    raw = re.sub(r"[\s\-().]", "", text or "")
    plus = raw.startswith("+")
    digits = raw[1:] if plus else raw
    if not digits.isdigit() or not 9 <= len(digits) <= 15:
        return None
    if not plus and len(digits) == 9:
        return f"+998{digits}"  # a local Uzbek number typed without the country code
    return f"+{digits}"


def clip(text: str, limit: int = ANSWER_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


async def choose_vacancy(session: AsyncSession, app: Application, vacancy_id: str | None) -> None:
    if vacancy_id is not None:
        vac = await session.get(Vacancy, vacancy_id)
        if vac is None or vac.status != "open":
            raise ApplyError("vacancy_closed")
        if app.telegram_user_id is not None:
            same = (await session.execute(select(Application.id).where(
                Application.telegram_user_id == app.telegram_user_id, Application.vacancy_id == vacancy_id,
                Application.status != "draft", Application.id != app.id,
            ).limit(1))).first()
            if same is not None:
                raise ApplyError("duplicate")
    app.vacancy_id = vacancy_id


# --------------------------------------------------------------------------- the test

def draw_logic(app: Application, rng: random.Random) -> None:
    app.logic = [{"qid": q.qid, "choice": None, "correct": None, "seconds": None}
                 for q in rng.sample(BANK, LOGIC_COUNT)]


def current_logic(app: Application) -> int | None:
    return next((i for i, row in enumerate(app.logic or []) if row.get("choice") is None), None)


def answer_logic(app: Application, index: int, choice: int, now: datetime) -> bool:
    """Only the question being asked takes an answer, once: an old or doubled tap changes nothing."""
    if index != current_logic(app) or not 0 <= choice < 4:
        return False
    started = as_utc(app.step_started_at) or now
    rows = [dict(r) for r in app.logic]
    rows[index].update(choice=choice, correct=choice == by_id(rows[index]["qid"]).answer,
                       seconds=max(0, round((now - started).total_seconds())))
    app.logic = rows  # a new list, so the change is saved
    app.step_started_at = now
    return True


def logic_score(app: Application) -> tuple[int, int]:
    rows = app.logic or []
    return sum(1 for r in rows if r.get("correct")), len(rows)


def current_job(app: Application) -> int | None:
    return next((i for i, row in enumerate(app.job or []) if row.get("a") is None), None)


def answer_job(app: Application, text: str) -> bool:
    index = current_job(app)
    if index is None:
        return False
    rows = [dict(r) for r in app.job]
    rows[index]["a"] = clip(text)
    app.job = rows
    return True


# --------------------------------------------------------------------------- submitting and deciding

async def _next_number(session: AsyncSession, now: datetime) -> str:
    period = local(now).strftime("%Y-%m")
    counter = await session.get(ApplicationCounter, period)
    if counter is None:
        counter = ApplicationCounter(period=period, last=0)
        session.add(counter)
    counter.last += 1
    await session.flush()
    return f"A-{period}-{counter.last:04d}"


async def submit(session: AsyncSession, app: Application, now: datetime) -> None:
    app.number = await _next_number(session, now)
    app.status, app.step, app.submitted_at = "submitted", "done", now
    await session.flush()
    await audit.record(session, "application.submit", "submitted", object_type="application", object_id=app.id)


@dataclass
class Decision:
    ok: bool
    status: str
    decided_by_name: str | None


async def _conditional(session: AsyncSession, app_id: str, allowed: tuple[str, ...], values: dict) -> bool:
    """One UPDATE guarded by the current status: of two people pressing at once, only the first changes it."""
    result = await session.execute(
        update(Application).where(Application.id == app_id, Application.status.in_(allowed)).values(**values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


async def _current(session: AsyncSession, app_id: str) -> Decision:
    app = await session.get(Application, app_id, populate_existing=True)
    who = await session.get(Employee, app.decided_by) if app and app.decided_by else None
    return Decision(False, app.status if app else "missing", who.full_name if who else None)


async def decide(session: AsyncSession, app_id: str, actor: Employee, action: str) -> Decision:
    if action not in DECISIONS:
        raise ValueError(action)
    status = "interview" if action == "interview" else "rejected"
    ok = await _conditional(session, app_id, DECISIONS[action],
                            {"status": status, "decided_by": actor.id, "decided_at": now_utc()})
    await audit.record(session, "application.decide", "done" if ok else "too_late", actor_id=actor.id,
                       object_type="application", object_id=app_id, detail=action)
    if not ok:
        return await _current(session, app_id)
    await session.get(Application, app_id, populate_existing=True)
    return Decision(True, status, actor.full_name)


def can_hire(app: Application) -> bool:
    return app.status in HIRE_FROM


async def mark_hired(session: AsyncSession, app_id: str, actor: Employee, employee_id: str | None) -> bool:
    ok = await _conditional(session, app_id, HIRE_FROM, {
        "status": "hired", "decided_by": actor.id, "decided_at": now_utc(), "employee_id": employee_id})
    await audit.record(session, "application.decide", "done" if ok else "too_late", actor_id=actor.id,
                       object_type="application", object_id=app_id, detail="hire")
    if ok:
        await session.get(Application, app_id, populate_existing=True)
    return ok


async def create_manual(session: AsyncSession, *, actor: Employee, full_name: str, phone: str,
                        vacancy_id: str | None, note: str) -> Application:
    """A walk-in applicant HR types in. No test, no broadcast: HR already knows about them."""
    now = now_utc()
    app = Application(source="manual", full_name=full_name, phone=phone, vacancy_id=vacancy_id,
                      note=clip(note), logic=[], job=[], answer_notes=[], reported_at=now)
    session.add(app)
    await session.flush()
    await submit(session, app, now)
    await audit.record(session, "application.manual", "created", actor_id=actor.id, object_type="application",
                       object_id=app.id)
    return app
