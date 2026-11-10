"""Vacancies: the starting list from the hiring site, the public list, closing and editing."""

from __future__ import annotations

import pytest

from hari.apply.vacancies import (
    create_vacancy, list_open, make_key, public_list, seed_vacancies, set_vacancy_status, title_for, update_vacancy,
)
from hari.config import Settings


async def test_seed_loads_site_vacancies_once(session_scope):
    async with session_scope() as s:
        assert await seed_vacancies(s) == 7
        assert await seed_vacancies(s) == 0
        keys = [v.key for v in await list_open(s)]
    assert keys == ["callcenter", "service", "retail", "b2b", "finance", "accountant", "mobilograph"]


async def test_public_list_is_titles_only_in_latin_ru_en(session_scope):
    async with session_scope() as s:
        await seed_vacancies(s)
        await create_vacancy(s, title="Омборчи", actor_id=None, look_for="SECRET", question_1="Q?")
        rows = public_list(await list_open(s))
    assert set(rows[0]) == {"key", "uz", "ru", "en"}
    assert rows[0]["ru"] == "Оператор call-центра" and rows[0]["uz"].startswith("Call-markaz")
    assert "SECRET" not in str(rows) and "Q?" not in str(rows)
    assert rows[-1] == {"key": "omborchi", "uz": "Omborchi", "ru": "Omborchi", "en": "Omborchi"}


async def test_closed_vacancy_leaves_open_list_and_edit_rules(session_scope):
    async with session_scope() as s:
        await seed_vacancies(s)
        vac = (await list_open(s))[0]
        await set_vacancy_status(s, vac.id, "closed", actor_id=None)
        assert vac.key not in [v.key for v in await list_open(s)]
        await update_vacancy(s, vac.id, "title_en", "-", actor_id=None)
        assert vac.title_en == ""
        with pytest.raises(ValueError):
            await update_vacancy(s, vac.id, "status", "open", actor_id=None)
        with pytest.raises(ValueError):
            await update_vacancy(s, vac.id, "title", "-", actor_id=None)


async def test_keys_stay_unique_and_titles_follow_language(session_scope):
    async with session_scope() as s:
        a = await create_vacancy(s, title="Омборчи", actor_id=None)
        b = await create_vacancy(s, title="Омборчи", actor_id=None, title_ru="Кладовщик")
    assert (a.key, b.key) == ("omborchi", "omborchi-2")
    assert title_for(b, "ru") == "Кладовщик" and title_for(a, "ru") == "Омборчи"
    assert title_for(a, "uz_latn") == "Omborchi" and title_for(a, "uz_cyrl") == "Омборчи"
    assert title_for(None, "ru") == "Общая заявка" and title_for(None, "uz_cyrl") == "Умумий ариза"
    assert make_key("B2B савдо менежери") == "b2b-savdo-menejeri"


def test_owner_ids_setting():
    assert Settings(_env_file=None, hari_owners=" 8632661510, x ,12 ").owner_ids == [8632661510, 12]
    assert Settings(_env_file=None).owner_ids == []
    assert Settings(_env_file=None).apply_reply_days == 3
