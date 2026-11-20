"""Live numbers from HARI's own database, for questions like «нечта одам базада бор?» that no company
document answers.

Only counts and categories — never a name, phone, Telegram ID or what anyone wrote — so they can go to the
AI provider like any other masked text (spec 2.0 §10, T30). HR and owners get the company figures; everyone
else only their own (their plan's progress, their open requests). The server counts; the AI only reads.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import cases as C
from hari import onboarding as O
from hari.access import can_manage_employees
from hari.db.models import Application, Employee, HRCase, OnboardingPlan, Vacancy, as_utc
from hari.worktime import fmt

OPEN_CASE = ("submitted", "accepted", "in_review", "decided")


async def facts_for(session: AsyncSession, emp: Employee, now: datetime) -> dict:
    out: dict = {"as_of": fmt(now)}
    plan = await O.get_plan(session, emp.id)
    if plan is not None:
        done, total = O.progress(plan)
        out["my_onboarding_steps"] = {"accepted": done, "total": total}
    mine = (await session.execute(select(HRCase.status).where(HRCase.requester_id == emp.id))).scalars().all()
    out["my_open_hr_requests"] = sum(1 for s in mine if s in OPEN_CASE)
    if not can_manage_employees(emp):
        return out

    people = (await session.execute(select(Employee))).scalars().all()
    current = [p for p in people if p.status != "archived"]
    out["employees"] = {
        "in_database": len(current),
        "archived": len(people) - len(current),
        "linked_to_telegram": sum(1 for p in current if p.telegram_user_id),
        "not_linked_yet": sum(1 for p in current if not p.telegram_user_id),
        "by_status": dict(Counter(p.status for p in current)),
        "by_role": dict(Counter(p.role for p in current)),
        "by_department": dict(Counter(p.department or "—" for p in current)),
    }
    plans = (await session.execute(select(OnboardingPlan.status))).scalars().all()
    out["onboarding_plans"] = dict(Counter(plans))
    # Only requests this person may see: a complaint about the owner is not even a number to the owner.
    cases = [c for c in (await session.execute(select(HRCase))).scalars().all() if C.can_see_case(emp, c)]
    open_cases = [c for c in cases if c.status in OPEN_CASE]
    out["hr_requests"] = {
        "open": len(open_cases),
        "first_answer_overdue": sum(1 for c in open_cases if c.status == "submitted" and c.first_response_due
                                    and as_utc(c.first_response_due) < now),
        "closed": sum(1 for c in cases if c.status == "closed"),
        "open_by_type": dict(Counter(c.title for c in open_cases)),
    }
    apps = (await session.execute(select(Application.status).where(Application.status != "draft"))).scalars().all()
    out["job_applications"] = {"total": len(apps), "by_status": dict(Counter(apps))}
    out["open_vacancies"] = len((await session.execute(select(Vacancy.id).where(Vacancy.status == "open"))).all())
    return out
