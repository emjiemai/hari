"""Applicants: `t.me/<bot>?start=apply` (or apply_garmin, apply_primus, …) opens a job application; so does
writing «ариза» / «ишга кирмоқчиман» to HARI without a card.

There are no buttons: each choice is listed with numbers and the applicant writes the number (or the
words of the option); yes/no questions take «ҳа» / «йўқ». The application's progress is the `step` saved
in the database, never FSM memory, so a redeploy in the middle loses nothing. This router runs first; its
filters only match people who are not staff, so employees, invite links and strangers without an
application pass through untouched.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from loguru import logger

from hari import egress, retention, voice
from hari import intent as I
from hari.apply import applications as A
from hari.apply import evaluate as E
from hari.apply.logic_bank import by_id, option_labels, question_text
from hari.apply.report import evaluate_and_report, recipients
from hari.apply.texts import LANG_CHOICES, t
from hari.apply.vacancies import list_open, title_for
from hari.bot.common import staff_by_tg
from hari.bot.ui import NO_KEYBOARD, notify
from hari.config import settings
from hari.db.models import Application, Vacancy
from hari.db.session import session_scope
from hari.translit import cyrillic_to_latin, normalize
from hari.worktime import local, now_utc

router = Router(name="apply")

APPLY_ARGS = re.compile(r"^apply(_[A-Za-z0-9_-]{0,40})?$")
TEXT_STEPS = {"name", "phone", "experience", "job"}
YES = ("ҳа", "ха", "розиман", "рози", "да", "согласен", "согласна", "ок", "майли", "хоп", "xa", "ha", "давом",
       "албатта", "конечно", "продолж", "хорошо", "ладно", "ҳўп", "бўпти", "бўлди", "go")
NO = ("йўқ", "йук", "нет", "не", "розимасман", "не согласен", "не согласна")
SKIP = ("йўқ", "йук", "нет", "ўтказиб", "ўтказ", "пропустить", "пропуск", "резюмем йўқ", "резюме йўқ", "нет резюме")
AGAIN = ("янги ариза", "бошқа вакансия", "яна ариза", "новая заявка", "другая вакансия", "ещё заявка")
DELETE = ("ўчир", "ўчиринг", "удал")

Reply = tuple[str, object | None]  # text, reply markup


class Applicant(BaseFilter):
    """A non-staff account with a draft (or, with `submitted_too`, any application). Hands the handler `app_id`."""

    def __init__(self, submitted_too: bool = False) -> None:
        self.submitted_too = submitted_too

    async def __call__(self, event: Message) -> bool | dict:
        tg = event.from_user.id
        async with session_scope() as session:
            if await staff_by_tg(session, tg) is not None:
                return False
            app = await A.draft_for(session, tg)
            if app is None and self.submitted_too:
                app = await A.last_submitted(session, tg)
        return {"app_id": app.id} if app is not None else False


def _words(text: str) -> str:
    return " ".join(normalize((text or "").lower()).split())


def _says(text: str, options: tuple[str, ...]) -> bool:
    words = _words(text)
    return any(re.search(rf"(?<!\w){re.escape(_words(o))}", words) for o in options if _words(o))


LETTERS = "АБВГ"  # options are lettered: many answers are numbers themselves ("3", "12")


def logic_choice(text: str, labels: tuple[str, ...]) -> int | None:
    """The option by its letter (А/Б/В/Г, or A/B/V/G) or by its whole text."""
    words = _words(text)
    if len(words) == 1 and words in LETTERS.lower():
        return LETTERS.lower().index(words)
    exact = [i for i, label in enumerate(labels) if _words(label) == words]
    return exact[0] if len(exact) == 1 else None


JOB_QUESTIONS = 3  # two work questions and the work-conditions one, before they are written

RUSSIAN = ("здравствуй", "привет", "добрый", "хочу", "работ", "русск", "на русском", "можно", "спасибо")


def detect_language(text: str) -> str | None:
    """The language and script someone wrote in: Latin letters → Uzbek Latin; ў қ ғ ҳ → Uzbek Cyrillic;
    ы, щ or common Russian words → Russian; other Cyrillic → Uzbek Cyrillic. No letters → None."""
    letters = [c for c in text or "" if c.isalpha()]
    if not letters:
        return None
    if sum("a" <= c.lower() <= "z" for c in letters) > len(letters) / 2:
        return "uz_latn"
    low = text.lower()
    if any(c in low for c in "ўқғҳ"):
        return "uz_cyrl"
    if any(c in low for c in "ыщ") or any(w in low for w in RUSSIAN):
        return "ru"
    return "uz_cyrl"


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {label}" for i, label in enumerate(items, start=1))


# --------------------------------------------------------------------------- what to say at each step

@dataclass
class Ask:
    """What HARI asks at a step. `how`: "fixed" goes out as written (the language question, the consent,
    a status); "voice" — the AI says `text` in its own words (hari.voice) and `block` follows unchanged;
    "lead" — the AI only adds a short reaction to the last answer, and `text` + `block` stay exactly as
    they are (test and work questions are the same for every applicant)."""

    text: str
    block: str = ""
    how: str = "fixed"
    markup: object | None = None


async def prompt(session, app: Application) -> Ask:  # noqa: ANN001
    lang = app.language
    total = len(app.logic or []) + (len(app.job) if app.job else JOB_QUESTIONS)
    if app.step == "lang":
        return Ask(t(lang, "choose_lang"), markup=NO_KEYBOARD)
    if app.step == "consent":
        return Ask(t(lang, "consent"))
    if app.step == "name":
        return Ask(t(lang, "ask_name"), how="voice")
    if app.step == "phone":
        return Ask(t(lang, "ask_phone"), how="voice")
    if app.step == "vacancy":
        options = await vacancy_options(session, lang)
        if app.telegram_user_id:
            _shown[app.telegram_user_id] = options
        return Ask(t(lang, "ask_vacancy"), _numbered([title for _, title in options]), how="voice")
    if app.step == "experience":
        return Ask(t(lang, "ask_experience"), how="voice")
    if app.step == "resume":
        return Ask(t(lang, "ask_resume"), how="voice")
    if app.step == "logic":
        index = A.current_logic(app)
        q = by_id(app.logic[index]["qid"])
        options = "\n".join(f"{LETTERS[i]}) {label}" for i, label in enumerate(option_labels(q, lang)))
        return Ask(t(lang, "logic_q", n=index + 1, total=total, text=question_text(q, lang),
                     options=cyrillic_to_latin(options) if lang == "uz_latn" else options), how="lead")
    if app.step == "job":
        index = A.current_job(app)
        row = app.job[index]
        if A.asking_follow_up(app):
            return Ask(row["fq"])  # written by the AI for this answer already
        if row.get("kind") == "conditions":
            return Ask(row["q"], how="voice")
        return Ask(t(lang, "job_q", n=len(app.logic or []) + index + 1, total=total, text=row["q"]), how="lead")
    text, markup = status_reply(app)
    return Ask(text, markup=markup)


# The vacancy list each applicant last saw, so "2" means the second line THEY saw even if a vacancy closed
# since. Process memory only: after a restart the current list is used (a closed one is refused anyway).
_shown: dict[int, list[tuple[str, str]]] = {}


async def vacancy_options(session, lang: str) -> list[tuple[str, str]]:  # noqa: ANN001
    """(vacancy id or "general", title) in the order they are listed."""
    rows = [(v.id, title_for(v, lang)) for v in await list_open(session)]
    return rows + [("general", title_for(None, lang))]


def status_reply(app: Application) -> Reply:
    when = local(app.submitted_at).strftime("%d.%m.%Y") if app.submitted_at else ""
    return (t(app.language, f"status_{app.status}", date=when) + "\n\n" + t(app.language, "status_hint"), None)


async def send(target: Message, replies: list[Reply]) -> None:
    for text, markup in replies:
        await target.answer(text, reply_markup=markup)


async def ensure_questions(bot: Bot, chat_id: int, app_id: str) -> None:
    """Draw the logic questions / write the job questions when a step needs them and has none yet.
    The AI call runs outside any database session."""
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        if app.step == "logic" and not app.logic:
            A.draw_logic(app, random.Random())
            A.go(app, "logic", now_utc())
        need_job = app.step == "job" and not app.job
        vac = await session.get(Vacancy, app.vacancy_id) if need_job and app.vacancy_id else None
        lang = app.language
    if not need_job:
        return
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    questions, origin = await E.job_questions(vac, lang)
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        if not app.job:
            # Spec §7 "иш шартларига мослик": when they can start, which days and hours — asked last, not scored.
            app.job = [{"q": q, "a": None} for q in questions] + [
                {"q": t(lang, "conditions_q"), "a": None, "kind": "conditions"}]
            app.job_origin = origin


async def show_step(target: Message, bot: Bot, app_id: str, before: list[Reply] | None = None,
                    said: str | None = None) -> None:
    """Ask what comes next. `said`: what the applicant just wrote, so HARI can react to it like a person.
    Only after consent does anything of theirs reach the AI, masked and without protected traits."""
    await ensure_questions(bot, target.chat.id, app_id)
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        ask = await prompt(session, app)
        lang, known = app.language, (app.full_name or "", app.phone or "")
        consented = app.consent_at is not None
    text = ask.text
    if ask.how != "fixed" and consented and not settings.hari_paused:
        await bot.send_chat_action(target.chat.id, ChatAction.TYPING)
        heard = egress.outbound(egress.redact_protected(said or ""), known=[k for k in known if k])[:600]
        text = await voice.interviewer(heard, text if ask.how == "voice" else "", lang, keep=text)
    if ask.block:
        text = f"{text}\n{ask.block}"
    await send(target, (before or []) + [(text, ask.markup)])


# --------------------------------------------------------------------------- entry

@router.message(CommandStart(deep_link=True, magic=F.args.regexp(APPLY_ARGS)))
async def apply_link(message: Message, command: CommandObject, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    tg = message.from_user.id
    async with session_scope() as session:
        emp = await staff_by_tg(session, tg)
        if emp is not None:
            await message.answer(t(emp.language, "already_staff"), reply_markup=NO_KEYBOARD)
            return
        if await A.draft_for(session, tg) is None:
            done = await A.last_submitted(session, tg)
            if done is not None:
                await send(message, [status_reply(done)])
                return
        app, created = await A.start_or_resume(session, tg, command.args)
        app_id = app.id
    if created:
        logger.info("Application started ({})", "vacancy link" if command.args else "no link")
    await show_step(message, bot, app_id)


async def start_application(message: Message, bot: Bot) -> None:
    """Someone without a card wrote that they want a job (or «янги ариза» after an earlier one)."""
    tg = message.from_user.id
    async with session_scope() as session:
        emp = await staff_by_tg(session, tg)
        if emp is None:
            app, _ = await A.start_or_resume(session, tg, "apply")
            app_id = app.id
    if emp is not None:
        await message.answer(t(emp.language, "already_staff"))
        return
    await show_step(message, bot, app_id)


@router.message(CommandStart(magic=F.args.is_(None)), Applicant(submitted_too=True))
async def plain_start(message: Message, state: FSMContext, bot: Bot, app_id: str) -> None:
    await state.clear()
    await show_step(message, bot, app_id)


@router.message(Command("bekor", "cancel"), Applicant())
async def cancel(message: Message, app_id: str) -> None:
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        lang = app.language
    await message.answer(t(lang, "cancelled"), reply_markup=NO_KEYBOARD)


async def ask_deletion(message: Message, bot: Bot) -> None:
    """The applicant asks for their data to be deleted. A person at HR decides; a legal hold stops it
    (spec 2.0 §10, T24)."""
    tg = message.from_user.id
    async with session_scope() as session:
        if await staff_by_tg(session, tg) is not None:
            return
        app = await A.draft_for(session, tg) or await A.last_submitted(session, tg)
        if app is None:
            return
        lang = app.language
        if app.status == "draft":  # nothing was submitted: the draft simply goes
            await session.delete(app)
            await message.answer(t(lang, "no_consent"))
            return
        fresh = await retention.request_deletion(session, app)
        held, number = bool(app.legal_hold), app.full_name or "Бир номзод"
        people = await recipients(session) if fresh else []
    await message.answer(t(lang, "delete_hold" if held else "delete_requested" if fresh else "delete_waiting"))
    for person in people:
        await notify(bot, person, f"{number} шахсий маълумотини ўчиришни сўради."
                     + (" Legal hold ўрнатилган — юрист кўриб чиқиши керак." if held else ""))


# --------------------------------------------------------------------------- answers, typed or sent

async def _choice_step(session, app: Application, text: str, now,  # noqa: ANN001
                       vacancy_hint: str | None = None) -> tuple[bool, list[Reply]]:
    """The steps that used to be buttons. (understood, replies to send before the next step)."""
    lang = app.language
    if app.step == "lang":
        # "ruscha", "o'zbekcha", "1", or simply whatever they wrote, in the language they wrote it in.
        picked = I.choice(text, list(LANG_CHOICES.items())) or detect_language(text)
        if picked is None:
            return False, [(t(lang, "ask_language_again"), None)]
        app.language = picked
        A.go(app, "consent", now)
        return True, []
    if app.step == "consent":
        if _says(text, NO) and not _says(text, ("розиман", "согласен", "согласна", "давом")):
            await session.delete(app)
            return True, [(t(lang, "no_consent"), None)]
        if _says(text, YES) or I.is_yes(text):
            app.consent_at = now
            A.go(app, "name", now)
            return True, []
        return False, [(t(lang, "yes_or_no"), None)]
    if app.step == "vacancy":
        options = _shown.get(app.telegram_user_id) or await vacancy_options(session, lang)
        picked = I.choice(text, [(vid, (title, *[w for w in title.split() if len(w) >= 5]))
                                 for vid, title in options]) or vacancy_hint
        if picked is None or picked not in {vid for vid, _ in options}:
            return False, [(t(lang, "pick_number"), None)]
        try:
            await A.choose_vacancy(session, app, None if picked == "general" else picked)
            A.go(app, "experience", now)
            return True, []
        except A.ApplyError as exc:  # it closed or was already applied for: say so, then the list again
            return True, [(t(lang, str(exc)), None)]
    if app.step == "resume":
        if _says(text, SKIP):
            A.go(app, "logic", now)
            return True, []
        return False, [(t(lang, "resume_please"), None)]
    if app.step == "logic":
        index = A.current_logic(app)
        q = by_id(app.logic[index]["qid"])
        picked = logic_choice(text, option_labels(q, lang))
        if picked is None or not A.answer_logic(app, index, picked, now):
            return False, [(t(lang, "pick_letter"), None)]
        if A.current_logic(app) is None:
            A.go(app, "job", now)
        return True, []
    return False, []


@router.message(~F.text.startswith("/"), Applicant(submitted_too=True))  # commands pass through
async def on_message(message: Message, bot: Bot, app_id: str) -> None:
    text = (message.text or "").strip()
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        if app.status != "draft":
            if text and _says(text, AGAIN):
                again = True
            elif text and _says(text, DELETE):
                again = None
            else:
                await send(message, [status_reply(app)])
                return
        else:
            again = False
    if again:
        await start_application(message, bot)
        return
    if again is None:
        await ask_deletion(message, bot)
        return
    vacancy_hint = await _vacancy_by_ai(app_id, text) if text else None
    answered: tuple[int, str] | None = None  # a work question just answered: (row, question) — may get a follow-up
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        lang, step, now = app.language, app.step, now_utc()
        before: list[Reply] = []
        if step in ("lang", "consent", "vacancy", "logic") or (step == "resume" and text):
            if not text:
                await message.answer(t(lang, "type_please"))
                return
            understood, before = await _choice_step(session, app, text, now, vacancy_hint)
            if not understood:
                await send(message, before)
                return
            if step == "consent" and app.consent_at is None:  # declined: the draft is gone
                await send(message, before)
                return
        elif step == "phone" and message.contact is not None:
            if message.contact.user_id != message.from_user.id:
                await message.answer(t(lang, "not_own_contact"))
                return
            phone = A.clean_phone(message.contact.phone_number)
            if phone is None:
                await message.answer(t(lang, "bad_phone"))
                return
            app.phone = phone
        elif step == "resume" and (message.document or message.photo):
            app.resume_file_id = (f"document:{message.document.file_id}" if message.document
                                  else f"photo:{message.photo[-1].file_id}")
        elif step == "resume":
            await message.answer(t(lang, "resume_please"))
            return
        elif step in TEXT_STEPS and not text:
            await message.answer(t(lang, "type_please"))
            return
        elif step == "name":
            name, phone = A.name_and_phone(text)  # "Мен Алиев Вали, тел 90 123 45 67" gives both
            if name is None:
                await message.answer(t(lang, "bad_name"))
                return
            app.full_name = name
            if phone:
                app.phone = phone
        elif step == "phone":
            phone = A.clean_phone(text)
            if phone is None:
                await message.answer(t(lang, "bad_phone"))
                return
            app.phone = phone
        elif step == "experience":
            app.experience = A.clip(text)
        elif step == "job":
            index = A.current_job(app)
            row = app.job[index] if index is not None else {}
            if index is not None and row.get("kind") != "conditions" and not A.asking_follow_up(app):
                answered = (index, row["q"])
            A.answer_job(app, text)

        if step == "name" and app.phone:
            A.go(app, "vacancy", now)
            before = [(t(lang, "phone_saved", phone=app.phone), None)]
        elif step == "name":
            A.go(app, "phone", now)
        elif step == "phone":
            A.go(app, "vacancy", now)
            before = [(t(lang, "phone_saved", phone=app.phone), None)]
        elif step == "experience":
            A.go(app, "resume", now)
        elif step == "resume" and app.step == "resume":
            A.go(app, "logic", now)
        elif step == "job" and A.current_job(app) is None:
            await A.submit(session, app, now)
        finished = app.status == "submitted"
        lang = app.language
    if finished:
        await message.answer(t(lang, "thanks", days=settings.apply_reply_days))
        await evaluate_and_report(bot, app_id)
        return
    if answered is not None:  # a thin answer gets one clarifying question, like an interviewer would ask
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        async with session_scope() as session:
            app = await session.get(Application, app_id)
            known = tuple(k for k in (app.full_name, app.phone) if k)
        question = await E.follow_up(answered[1], text, lang, known)
        if question:
            async with session_scope() as session:
                A.add_follow_up(await session.get(Application, app_id), answered[0], question)
    await show_step(message, bot, app_id, before, said=text)


async def _vacancy_by_ai(app_id: str, text: str) -> str | None:
    """At the vacancy step, words the list cannot match ("продавцом хочу") go to the AI with the titles."""
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        if app is None or app.step != "vacancy" or app.consent_at is None:
            return None
        options = _shown.get(app.telegram_user_id) or await vacancy_options(session, app.language)
        lang, known = app.language, tuple(k for k in (app.full_name, app.phone) if k)
    if I.choice(text, [(vid, (title, *[w for w in title.split() if len(w) >= 5])) for vid, title in options]):
        return None  # the list already understands it
    index = await E.pick_vacancy(text, [title for _, title in options], lang, known)
    return options[index][0] if index is not None else None
