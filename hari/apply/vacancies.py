"""Vacancies: what the hiring pages show and what applicants pick in the bot.

Titles are public (the Primus and Garmin pages list them). "What we look for" and the job questions are
for HARI and staff only and never leave the server through the public list.
"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit
from hari.db.models import Vacancy
from hari.translit import cyrillic_to_latin

# The vacancies on the hiring site (D:\MGM\hire_qr\assets\app.js) when this was built.
SEED: list[dict] = [
    {"key": "callcenter", "title": "Call-марказ оператори",
     "title_ru": "Оператор call-центра", "title_en": "Call center operator"},
    {"key": "service", "title": "Техник сервис мутахассиси",
     "title_ru": "Специалист технического сервиса", "title_en": "Technical service specialist"},
    {"key": "retail", "title": "Дўкон сотувчи-консультанти",
     "title_ru": "Продавец-консультант (розница)", "title_en": "Retail sales associate"},
    {"key": "b2b", "title": "B2B савдо менежери",
     "title_ru": "Менеджер по B2B-продажам", "title_en": "B2B sales manager"},
    {"key": "finance", "title": "Молиячи", "title_ru": "Финансист", "title_en": "Financial specialist"},
    {"key": "accountant", "title": "Бухгалтер", "title_ru": "Бухгалтер", "title_en": "Accountant"},
    {"key": "mobilograph", "title": "Мобилограф", "title_ru": "Мобилограф", "title_en": "Mobile videographer"},
]

GENERAL = {"uz_cyrl": "Умумий ариза", "ru": "Общая заявка"}
EDITABLE = ("title", "title_ru", "title_en", "department", "description", "look_for", "question_1", "question_2")


async def seed_vacancies(session: AsyncSession) -> int:
    """Load the site's vacancies into an empty table. Never touches a table that has any row."""
    if (await session.execute(select(func.count()).select_from(Vacancy))).scalar_one():
        return 0
    for order, row in enumerate(SEED):
        session.add(Vacancy(sort_order=(order + 1) * 10, **row))
    await session.flush()
    return len(SEED)


async def list_open(session: AsyncSession) -> list[Vacancy]:
    return list((await session.execute(
        select(Vacancy).where(Vacancy.status == "open").order_by(Vacancy.sort_order, Vacancy.created_at)
    )).scalars())


async def list_all(session: AsyncSession) -> list[Vacancy]:
    rows = (await session.execute(select(Vacancy).order_by(Vacancy.sort_order, Vacancy.created_at))).scalars()
    return sorted(rows, key=lambda v: v.status != "open")  # stable: open first, same order inside


def title_for(vac: Vacancy | None, lang: str) -> str:
    if vac is None:
        return GENERAL["ru"] if lang == "ru" else (
            cyrillic_to_latin(GENERAL["uz_cyrl"]) if lang == "uz_latn" else GENERAL["uz_cyrl"])
    if lang == "ru":
        return vac.title_ru or vac.title
    return cyrillic_to_latin(vac.title) if lang == "uz_latn" else vac.title


def public_list(vacs: list[Vacancy]) -> list[dict]:
    rows = []
    for v in vacs:
        uz = cyrillic_to_latin(v.title)
        rows.append({"key": v.key, "uz": uz, "ru": v.title_ru or uz, "en": v.title_en or uz})
    return rows


def make_key(title: str) -> str:
    latin = cyrillic_to_latin(title).lower()
    key = re.sub(r"[^a-z0-9]+", "-", re.sub("[ʻʼ'`’‘]", "", latin)).strip("-")
    return key[:40] or "vacancy"


async def _free_key(session: AsyncSession, base: str) -> str:
    taken = set((await session.execute(select(Vacancy.key).where(Vacancy.key.like(f"{base}%")))).scalars())
    if base not in taken:
        return base
    n = 2
    while f"{base[:36]}-{n}" in taken:
        n += 1
    return f"{base[:36]}-{n}"


async def create_vacancy(session: AsyncSession, *, title: str, actor_id: str | None, **fields: str) -> Vacancy:
    title = " ".join(title.split())
    if not title:
        raise ValueError("title")
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    vac = Vacancy(key=await _free_key(session, make_key(title)), title=title, created_by=actor_id,
                  **{k: (v or "").strip() for k, v in fields.items()})
    session.add(vac)
    await session.flush()
    await audit.record(session, "vacancy.create", "created", actor_id=actor_id, object_type="vacancy", object_id=vac.id)
    return vac


async def set_vacancy_status(session: AsyncSession, vac_id: str, status: str, actor_id: str | None) -> Vacancy | None:
    if status not in ("open", "closed"):
        raise ValueError("status")
    vac = await session.get(Vacancy, vac_id)
    if vac is None:
        return None
    vac.status = status
    await audit.record(session, "vacancy.status", status, actor_id=actor_id, object_type="vacancy", object_id=vac.id)
    return vac


async def update_vacancy(session: AsyncSession, vac_id: str, field: str, value: str,
                         actor_id: str | None) -> Vacancy | None:
    """`-` clears a field. The Uzbek title can't be cleared."""
    if field not in EDITABLE:
        raise ValueError(f"field {field!r} can't be edited")
    value = "" if value.strip() == "-" else value.strip()
    if field == "title" and not value:
        raise ValueError("title can't be empty")
    vac = await session.get(Vacancy, vac_id)
    if vac is None:
        return None
    setattr(vac, field, value)
    await audit.record(session, "vacancy.edit", "updated", actor_id=actor_id, object_type="vacancy",
                       object_id=vac.id, detail=field)
    return vac
