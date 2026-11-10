"""Applicant texts and the report the boss and HR receive."""

from __future__ import annotations

from hari.apply.report import recipients, report_buttons, report_parts, send_report
from hari.apply.texts import t
from hari.db.models import Application
from tests.conftest import FakeBot


def done_app(**kw) -> Application:
    base = dict(id="a1b2", number="A-2026-11-0007", full_name="Алиев Вали", phone="+998901234567", language="ru",
                experience="3 йил савдода", verdict="strong", reason="Аниқ ва мантиқли.", status="submitted",
                source="apply_garmin",
                logic=[{"qid": "series-1", "choice": 1, "correct": True, "seconds": 42}] * 3,
                job=[{"q": "Q1", "a": "Жавоб 1"}, {"q": "Q2", "a": "Жавоб 2"}], answer_notes=["n1", "n2"])
    base.update(kw)
    return Application(**base)


def test_report_puts_verdict_first():
    parts = report_parts(done_app(), "Савдо менежери")
    assert parts[0].startswith("Янги ариза A-2026-11-0007 — Савдо менежери")
    head = parts[0].split("\n")
    assert head[1] == "HARI баҳоси: Суҳбатга чақиришга арзийди" and head[2] == "Сабаб: Аниқ ва мантиқли."
    assert "Мантиқ: 3/3 (42 с, 42 с, 42 с)" in parts[0] and "Сайт: Garmin" in parts[0]
    assert "Тил: Русский" in parts[0] and "Изоҳ: n1" in parts[0]


def test_long_answers_are_split_and_nothing_lost():  # Review Focus 5
    app = done_app(experience="x" * 1500, job=[{"q": "Q1", "a": "a" * 1500}, {"q": "Q2", "a": "b" * 1500}])
    parts = report_parts(app, "Савдо менежери")
    assert all(len(p) <= 4000 for p in parts) and len(parts) >= 2
    assert parts[0].startswith("Янги ариза") and "Суҳбатга чақиришга арзийди" in parts[0]
    joined = "".join(parts)
    assert joined.count("a" * 1500) == 1 and joined.count("b" * 1500) == 1 and joined.count("x" * 1500) == 1


def test_no_ai_view_and_status_lines():
    parts = report_parts(done_app(verdict=None, reason=None, answer_notes=[], status="rejected", source="apply"),
                         "Умумий ариза")
    assert "HARI баҳоси: AI баҳоси йўқ" in parts[0] and "Сайт:" not in parts[0]
    assert "Ҳолат: Рад этилган" in parts[0]


def test_buttons_follow_status():
    labels = lambda app: [b.text for row in report_buttons(app).inline_keyboard for b in row]  # noqa: E731
    assert labels(done_app()) == ["Суҳбатга чақириш", "Рад этиш", "Ишга олиш"]
    assert labels(done_app(status="interview")) == ["Рад этиш", "Ишга олиш"]
    assert report_buttons(done_app(status="rejected")) is None and report_buttons(done_app(status="hired")) is None


def test_texts_in_three_languages():
    assert "Розиман" in t("uz_cyrl", "consent_btn") and "Roziman" in t("uz_latn", "consent_btn")
    assert t("ru", "consent_btn") == "Согласен(на)"
    assert "3" in t("ru", "thanks", days=3) and t("ru", "thanks", days=3) != t("uz_cyrl", "thanks", days=3)
    assert "+998901234567" in t("uz_cyrl", "msg_interview", phone="+998901234567")
    assert t("xx", "ask_name") == t("uz_cyrl", "ask_name")
    for key in ("choose_lang", "consent", "ask_phone", "type_please", "msg_reject", "already_staff"):
        assert t("ru", key) and t("uz_cyrl", key)


async def test_send_report_reaches_owners_and_hr_only(session_scope, staff):
    async with session_scope() as s:
        await staff(s, "Раҳбаров Бек", "owner", tg=1)
        await staff(s, "Кадрова Зайнаб", "hr", tg=2)
        await staff(s, "Ишчи Олим", tg=3)
        await staff(s, "Телеграмсиз Ҳур", "hr")
        app = done_app()
        s.add(app)
        await s.flush()
        assert {p.telegram_user_id for p in await recipients(s)} == {1, 2}
        bot = FakeBot()
        assert await send_report(bot, s, app) == 2
        assert {chat for chat, _ in bot.sent} == {1, 2} and app.reported_at is not None
