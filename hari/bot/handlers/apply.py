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

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from loguru import logger

from hari import intent as I
from hari import retention
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
YES = ("ҳа", "ха", "розиман", "рози", "да", "согласен", "согласна", "ок", "майли", "хоп", "xa", "ha")
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


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {label}" for i, label in enumerate(items, start=1))


# --------------------------------------------------------------------------- what to say at each step

async def prompt(session, app: Application) -> list[Reply]:  # noqa: ANN001
    lang = app.language
    if app.step == "lang":
        return [(t(lang, "choose_lang"), NO_KEYBOARD)]
    if app.step == "consent":
        return [(t(lang, "consent"), None)]
    if app.step == "name":
        return [(t(lang, "ask_name"), None)]
    if app.step == "phone":
        return [(t(lang, "ask_phone"), None)]
    if app.step == "vacancy":
        options = await vacancy_options(session, lang)
        if app.telegram_user_id:
            _shown[app.telegram_user_id] = options
        titles = [title for _, title in options]
        return [(t(lang, "ask_vacancy") + "\n" + _numbered(titles), None)]
    if app.step == "experience":
        return [(t(lang, "ask_experience"), None)]
    if app.step == "resume":
        return [(t(lang, "ask_resume"), None)]
    if app.step == "logic":
        index = A.current_logic(app)
        q = by_id(app.logic[index]["qid"])
        options = "\n".join(f"{LETTERS[i]}) {label}" for i, label in enumerate(option_labels(q, lang)))
        return [(t(lang, "logic_q", n=index + 1, text=question_text(q, lang),
                   options=cyrillic_to_latin(options) if lang == "uz_latn" else options), None)]
    if app.step == "job":
        index = A.current_job(app)
        return [(t(lang, "job_q", n=index + 4, text=app.job[index]["q"]), None)]
    return [status_reply(app)]


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
            app.job, app.job_origin = [{"q": q, "a": None} for q in questions], origin


async def show_step(target: Message, bot: Bot, app_id: str, before: list[Reply] | None = None) -> None:
    await ensure_questions(bot, target.chat.id, app_id)
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        replies = (before or []) + await prompt(session, app)
    await send(target, replies)


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
        held, number = bool(app.legal_hold), app.number
        people = await recipients(session) if fresh else []
    await message.answer(t(lang, "delete_hold" if held else "delete_requested" if fresh else "delete_waiting"))
    from hari.bot.handlers.hiring import deletion_hint  # here: hiring imports the staff handlers

    for person in people:
        await notify(bot, person, f"{number}: номзод шахсий маълумотини ўчиришни сўради."
                     + (" Legal hold ўрнатилган — юрист кўриб чиқиши керак." if held else
                        " " + deletion_hint(number)))


# --------------------------------------------------------------------------- answers, typed or sent

async def _choice_step(session, app: Application, text: str, now) -> tuple[bool, list[Reply]]:  # noqa: ANN001
    """The steps that used to be buttons. (understood, replies to send before the next step)."""
    lang = app.language
    if app.step == "lang":
        picked = I.choice(text, list(LANG_CHOICES.items()))
        if picked is None:
            return False, [(t(lang, "pick_number"), None)]
        app.language = picked
        A.go(app, "consent", now)
        return True, []
    if app.step == "consent":
        if _says(text, NO) and not _says(text, ("розиман", "согласен", "согласна")):
            await session.delete(app)
            return True, [(t(lang, "no_consent"), None)]
        if _says(text, YES):
            app.consent_at = now
            A.go(app, "name", now)
            return True, []
        return False, [(t(lang, "yes_or_no"), None)]
    if app.step == "vacancy":
        options = _shown.get(app.telegram_user_id) or await vacancy_options(session, lang)
        picked = I.choice(text, [(vid, (title,)) for vid, title in options])
        if picked is None:
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
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        lang, step, now = app.language, app.step, now_utc()
        before: list[Reply] = []
        if step in ("lang", "consent", "vacancy", "logic") or (step == "resume" and text):
            if not text:
                await message.answer(t(lang, "type_please"))
                return
            understood, before = await _choice_step(session, app, text, now)
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
            name = A.clean_name(text)
            if name is None:
                await message.answer(t(lang, "bad_name"))
                return
            app.full_name = name
        elif step == "phone":
            phone = A.clean_phone(text)
            if phone is None:
                await message.answer(t(lang, "bad_phone"))
                return
            app.phone = phone
        elif step == "experience":
            app.experience = A.clip(text)
        elif step == "job":
            A.answer_job(app, text)

        if step == "name":
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
    await show_step(message, bot, app_id, before)
