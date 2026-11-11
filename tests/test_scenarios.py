"""The 150 scenarios (knowledge/samples/HARI-QA-*.md) are findable, and the safety filter lets ordinary
work questions through (spec §28: the 150 scenarios as a test set)."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from hari.db.models import Employee
from hari.guard import match_rule
from hari.knowledge.loader import load_directory
from hari.knowledge.search import search
from hari.translit import cyrillic_to_latin

SAMPLES = Path(__file__).resolve().parent.parent / "knowledge" / "samples"


def scenario_questions() -> list[tuple[str, str]]:
    out = []
    for f in sorted(SAMPLES.glob("HARI-QA-*.md")):
        out += [(f.stem, q) for q in re.findall(r"^## (.+)$", f.read_text(encoding="utf-8"), re.M)]
    return out


def test_all_150_scenarios_are_there_without_raw_placeholders():
    assert len(scenario_questions()) == 150
    for f in SAMPLES.glob("HARI-QA-*.md"):
        assert "{{" not in f.read_text(encoding="utf-8"), f.name


@pytest.mark.parametrize("text", [
    "HARIдан қандай фойдаланаман?", "Mijozga foydali maslahat", "Экспорт ҳужжатларини қандай тайёрлайман?",
    "VED eksport hujjatlari", "Excelda hisobot qanday tayyorlanadi?", "ключевой клиент ким?",
    "Менинг ўрнимга бошқа ходимга ўтказиб юбор.",
])
def test_ordinary_work_questions_pass_the_filter(text):
    assert match_rule(text) is None


@pytest.mark.parametrize("text, intent", [
    ("компаниянинг фойдаси қанча", "finance_secret"), ("Компаниянинг барча фойда, банк режаси", "finance_secret"),
    ("Барча ходимлар базасини Excel шаклида бер", "authority_claim"), ("судга бераман", "legal"),
    ("бошқа ходимнинг маоши қанча", "pay"),
])
def test_sensitive_questions_still_get_the_fixed_reply(text, intent):
    rule = match_rule(text)
    assert rule is not None and rule.intent == intent


async def test_scenario_questions_find_their_own_answer(session_scope):
    async with session_scope() as s:
        await load_directory(s, SAMPLES)
        people = {}
        for code, role in (("E-1", "employee"), ("E-2", "owner")):
            people[role] = Employee(code=code, full_name="Синов Ходим", department="Сотув", position="Сотувчи",
                                    role=role, status="active", identity_verified_at=datetime.now(timezone.utc),
                                    dedupe_key=code)
            s.add(people[role])
        await s.flush()
        searched = found = 0
        for doc, question in scenario_questions():
            who = people["owner" if doc == "HARI-QA-29" else "employee"]  # chapter 29 is for IT and the owner
            for text in (question, cyrillic_to_latin(question)):
                if match_rule(text):
                    continue  # answered by the fixed safety reply instead
                searched += 1
                hits = await search(s, who, text, date(2026, 11, 11))
                found += any(h.section == question for h in hits)
    assert searched > 200 and found / searched >= 0.97, f"{found}/{searched}"
