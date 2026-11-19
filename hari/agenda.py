"""What each person should do now, worked out by HARI itself — so HARI can tell them, like an HR
coordinator would, instead of waiting to be asked (spec §1 "HR мувофиқлаштирувчи").

- `morning_brief`: one message each work morning for everyone who has something to do: their onboarding
  steps for today, step results waiting for their acceptance, HR requests waiting for their answer, and
  (HR and owners) applications waiting for a decision. Each item says what to write back. Things that
  arrived today are left out: the person was told when they arrived.
- `next_step`: after something is done, the one step that comes next for a newcomer.

Everything here is read from the database by the server; no AI writes or decides any of it. The scheduler
sends the brief through the outbox (work days and hours only, the daily limit, never twice).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import cases as C
from hari import onboarding as O
from hari.access import can_accept_step, can_manage_employees
from hari.db.models import Application, Employee, OnboardingPlan, OnboardingStep, Vacancy, as_utc
from hari.worktime import fmt, local, today_local

MAX_ITEMS = 6  # per section; the rest is counted, and the person can ask for the full list
BRIEF_HOURS = 3  # the brief goes out in the first hours of the person's work day, not in the afternoon


def first_name(emp: Employee) -> str:
    parts = emp.full_name.split()
    return parts[1] if len(parts) > 1 else emp.full_name


def step_line(n: int, step: OnboardingStep) -> str:
    """One step and what to write about it."""
    if step.status == "returned":
        why = f" Сабаб: {step.return_reason}." if step.return_reason else ""
        return f"{n}. «{step.title}» — қайтарилган.{why} Тузатиб, «{n} ни бажардим: …» деб ёзинг."
    if step.kind == "read":
        return f"{n}. «{step.title}» — ўқиш учун «{n} ни очинг», ўқиб бўлгач «{n} ни ўқидим» деб ёзинг."
    return f"{n}. «{step.title}» — бажаргач, «{n} ни бажардим: нима қилдингиз» деб ёзинг (фото ёки файл ҳам бўлади)."


async def _open_steps(session: AsyncSession, emp: Employee, today: date) -> list[tuple[int, OnboardingStep]]:
    plan = await O.get_plan(session, emp.id)
    if plan is None or plan.status != "active":
        return []
    due = {s.id for s in O.due_open_steps(emp, plan, today)}
    steps = [(n, s) for n, s in enumerate(plan.steps, start=1) if s.id in due]
    return sorted(steps, key=lambda ns: (ns[1].status != "returned", ns[0]))  # a returned step first


async def next_step(session: AsyncSession, emp: Employee, today: date | None = None) -> str | None:
    """The step a newcomer should do next, said the way HARI says it, or None."""
    steps = await _open_steps(session, emp, today or today_local())
    return f"Кейинги қадам: {step_line(*steps[0])}" if steps else None


async def queue_steps(session: AsyncSession, me: Employee) -> list[tuple[str, OnboardingStep, Employee]]:
    """Submitted step results this person may accept, as S1, S2… in the order they came."""
    steps = (await session.execute(select(OnboardingStep).where(OnboardingStep.status == "submitted")
                                   .order_by(OnboardingStep.submitted_at, OnboardingStep.id))).scalars().all()
    mine = []
    for s in steps:
        plan = await session.get(OnboardingPlan, s.plan_id)
        owner = await session.get(Employee, plan.employee_id) if plan else None
        if owner is not None and can_accept_step(me, s, owner):
            mine.append((f"S{len(mine) + 1}", s, owner))
    return mine


async def _my_step(session: AsyncSession, emp: Employee, step: OnboardingStep, owner: Employee) -> bool:
    acceptor = await O.acceptor_for(session, step, owner)
    return acceptor is not None and acceptor.id == emp.id


def _before(moment: datetime | None, today: date) -> bool:
    return moment is not None and local(as_utc(moment)).date() < today


def _section(title: str, lines: list[str], more_hint: str) -> list[str]:
    if not lines:
        return []
    out = [title] + [f"• {line}" for line in lines[:MAX_ITEMS]]
    if len(lines) > MAX_ITEMS:
        out.append(f"…ва яна {len(lines) - MAX_ITEMS} та — {more_hint}")
    return out


async def todo(session: AsyncSession, emp: Employee, now: datetime) -> list[str]:
    """The person's to-do as text blocks (empty when nothing waits for them)."""
    today = today_local(now)
    blocks: list[list[str]] = []

    steps = await _open_steps(session, emp, today)
    blocks.append(_section("Мослашув режангиздан:", [step_line(n, s) for n, s in steps], "«режам» деб ёзинг."))

    # Only what this person is responsible for: the results that were sent to them, the requests they hold.
    # (HR and owners MAY act on everyone's; their brief still lists only their own.)
    accept = [(ref, s, o) for ref, s, o in await queue_steps(session, emp)
              if _before(s.submitted_at, today) and await _my_step(session, emp, s, o)]
    blocks.append(_section(
        "Сизнинг қабулингизни кутаётган натижалар:",
        [f"{ref}. {o.full_name} — «{s.title}». «{ref} ни қабул қил» ёки «{ref} ни қайтар: нимани тузатиш керак»."
         for ref, s, o in accept], "«иш навбати» деб ёзинг."))

    cases = sorted(await C.open_for_handler(session, emp), key=lambda c: as_utc(c.first_response_due))
    case_lines = []
    for c in cases:
        if not _before(c.created_at, today):
            continue
        if c.assignee_id != emp.id and not (c.assignee_id is None and emp.role in ("hr", "owner")):
            continue  # someone else holds it
        late = c.status == "submitted" and c.first_response_due and as_utc(c.first_response_due) < now
        when = "⚠️ жавоб муддати ўтган" if late else (
            "жавоб берилган, иш очиқ" if c.status == "decided" else f"жавоб муддати {fmt(c.first_response_due)}")
        case_lines.append(f"{c.number} — {c.title}, {when}. Жавоб: «{c.number} га жавоб: …».")
    blocks.append(_section("Жавобингизни кутаётган мурожаатлар:", case_lines, "«иш навбати» деб ёзинг."))

    if can_manage_employees(emp):
        apps = (await session.execute(select(Application).where(
            Application.status.in_(("submitted", "interview"))).order_by(Application.submitted_at))).scalars().all()
        app_lines = []
        for a in apps:
            if not _before(a.submitted_at, today):
                continue
            vac = await session.get(Vacancy, a.vacancy_id) if a.vacancy_id else None
            where = f", {vac.title}" if vac else ""
            if a.status == "submitted":
                ask = f"кўриб чиқинг: «{a.number} ни кўрсат», кейин суҳбатга чақиринг ёки рад этинг"
            else:
                ask = f"суҳбат ўтган бўлса, баҳо қўйинг: «{a.number} суҳбат баҳоси 0–4», кейин ишга олинг ёки рад этинг"
            app_lines.append(f"{a.number} — {a.full_name or 'номзод'}{where}: {ask}.")
        asking = (await session.execute(select(Application).where(
            Application.deletion_requested_at.is_not(None), Application.purged_at.is_(None),
            Application.legal_hold.is_not(True)))).scalars().all()
        for a in asking:
            if _before(a.deletion_requested_at, today):
                app_lines.append(f"{a.number} — маълумотини ўчиришни сўраган: «{a.number} маълумотини ўчир» "
                                 f"ёки «{a.number} legal hold».")
        blocks.append(_section("Қарор кутаётган аризалар:", app_lines, "«аризалар» деб ёзинг."))

    return ["\n".join(b) for b in blocks if b]


async def morning_brief(session: AsyncSession, emp: Employee, now: datetime) -> str | None:
    """Today's message for this person, or None when nothing waits for them or it is not their morning."""
    loc = local(now)
    if not (emp.works_on(loc.weekday()) and emp.work_start_hour <= loc.hour < emp.work_start_hour + BRIEF_HOURS
            and loc.hour < emp.work_end_hour):
        return None
    blocks = await todo(session, emp, now)
    if not blocks:
        return None
    head = f"Хайрли тонг, {first_name(emp)}. Бугун сиздан кутилаётган ишлар:"
    tail = "Нимадир тушунарсиз бўлса ёки бажара олмасангиз, шу ерга ёзинг — ёрдам бераман."
    return "\n\n".join([head, *blocks, tail])
