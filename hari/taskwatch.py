"""Timers for the tasks colleagues give each other (hari.bot.handlers.tasks).

- How is it going: during the person's work hours, TASK_CHECK_HOURS after the last news about an open
  task (given, or the last thing they said about it), HARI asks them. At most once a day per task.
- The evening report: at TASK_REPORT_TIME everyone who gave tasks hears, for each one still open or
  finished today, what the person said and when. Nothing to tell → no message.
Both go through the outbox under a key for the day, so a restart never sends a second copy.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari.access import is_staff
from hari.config import settings
from hari.db.models import Employee, Task, TaskUpdate, as_utc
from hari.digest import _hhmm, _reached
from hari.outbox import queue
from hari.worktime import is_work_time, local, today_local


async def last_update(session: AsyncSession, task: Task) -> TaskUpdate | None:
    return (await session.execute(select(TaskUpdate).where(TaskUpdate.task_id == task.id)
                                  .order_by(TaskUpdate.at.desc()).limit(1))).scalar_one_or_none()


def _hm(dt: datetime) -> str:
    return local(dt).strftime("%H:%M")


async def check_ins(session: AsyncSession, now: datetime) -> list[str]:
    queued: list[str] = []
    today = today_local(now)
    wait = timedelta(hours=max(settings.task_check_hours, 1))
    for task in (await session.execute(select(Task).where(Task.status == "open").order_by(Task.at))).scalars():
        if task.asked_at and today_local(as_utc(task.asked_at)) == today:
            continue  # asked today already
        person = await session.get(Employee, task.to_id)
        if person is None or not person.telegram_user_id or not is_staff(person) or not is_work_time(person, now):
            continue
        update = await last_update(session, task)
        news = max([as_utc(task.at)] + ([as_utc(update.at)] if update else []))
        if now - news < wait:
            continue
        giver = await session.get(Employee, task.from_id)
        text = f"«{task.text}» топшириғи қандай кетяпти?" + (f" ({giver.full_name} берган)" if giver else "")
        msg = await queue(session, person, "task_check", f"taskcheck:{task.id}:{today}", text, now)
        if msg:
            task.asked_at = now
            queued.append(msg.id)
    return queued


async def report_lines(session: AsyncSession, giver: Employee, now: datetime) -> list[str]:
    today = today_local(now)
    lines: list[str] = []
    tasks = (await session.execute(select(Task).where(Task.from_id == giver.id).order_by(Task.at))).scalars()
    for task in tasks:
        done_today = task.status == "done" and task.done_at and today_local(as_utc(task.done_at)) == today
        if task.status != "open" and not done_today:
            continue
        person = await session.get(Employee, task.to_id)
        who = person.full_name if person else "ходим"
        if done_today:
            lines.append(f"• {who}: «{task.text}» — бажарилди ({_hm(as_utc(task.done_at))})"
                         + (f". Изоҳ: {task.done_text}" if task.done_text else ""))
            continue
        update = await last_update(session, task)
        if update is not None and today_local(as_utc(update.at)) == today:
            lines.append(f"• {who}: «{task.text}» — {update.text} ({_hm(as_utc(update.at))})")
        elif update is not None:
            lines.append(f"• {who}: «{task.text}» — бугун янгилик йўқ; охиргиси: {update.text} "
                         f"({local(as_utc(update.at)).strftime('%d.%m')})")
        else:
            lines.append(f"• {who}: «{task.text}» — ҳали ҳеч нарса ёзмади")
    return lines


async def evening(session: AsyncSession, now: datetime) -> list[str]:
    if not _reached(now, _hhmm(settings.task_report_time, (20, 0))):
        return []
    today = today_local(now)
    queued: list[str] = []
    givers = set((await session.execute(select(Task.from_id))).scalars())
    for giver_id in sorted(givers):
        giver = await session.get(Employee, giver_id)
        if giver is None or not giver.telegram_user_id or not is_staff(giver):
            continue
        lines = await report_lines(session, giver, now)
        if not lines:
            continue
        msg = await queue(session, giver, "task_report", f"taskreport:{giver.id}:{today}",
                          "Бугунги топшириқлар:\n" + "\n".join(lines), now)
        if msg:
            queued.append(msg.id)
    return queued
