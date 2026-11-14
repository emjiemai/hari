"""Applicants: `t.me/<bot>?start=apply` (or apply_garmin, apply_primus, …) opens a job application.

The application's progress is the `step` saved in the database, never FSM memory, so a redeploy in the
middle loses nothing. This router runs first; its filters only match people who are not staff, so
employees, invite links and strangers without an application pass through untouched.
"""

from __future__ import annotations

import random
import re

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove
from loguru import logger

from hari.apply import applications as A
from hari.apply import evaluate as E
from hari.apply.logic_bank import by_id, option_labels, question_text
from hari import retention
from hari.apply.report import evaluate_and_report, recipients
from hari.apply.texts import LANG_BUTTONS, t
from hari.apply.vacancies import list_open, title_for
from hari.bot.common import handles_work, staff_by_tg
from hari.bot.ui import ApplyCb, inline, main_menu, notify
from hari.config import settings
from hari.db.models import Application, Vacancy
from hari.db.session import session_scope
from hari.worktime import local, now_utc

router = Router(name="apply")

APPLY_ARGS = re.compile(r"^apply(_[A-Za-z0-9_-]{0,40})?$")
TEXT_STEPS = {"name", "phone", "experience", "job"}

Reply = tuple[str, object | None]  # text, markup


class Applicant(BaseFilter):
    """A non-staff account with a draft (or, with `submitted_too`, any application). Hands the handler `app_id`."""

    def __init__(self, submitted_too: bool = False) -> None:
        self.submitted_too = submitted_too

    async def __call__(self, event: Message | CallbackQuery) -> bool | dict:
        tg = event.from_user.id
        async with session_scope() as session:
            if await staff_by_tg(session, tg) is not None:
                return False
            app = await A.draft_for(session, tg)
            if app is None and self.submitted_too:
                app = await A.last_submitted(session, tg)
        return {"app_id": app.id} if app is not None else False


# --------------------------------------------------------------------------- what to say at each step

async def prompt(session, app: Application) -> list[Reply]:  # noqa: ANN001
    lang = app.language
    if app.step == "lang":
        return [(t(lang, "choose_lang"), inline([[(label, ApplyCb(action="lang", value=code))]
                                                  for code, label in LANG_BUTTONS.items()]))]
    if app.step == "consent":
        return [(t(lang, "consent"), inline([[(t(lang, "consent_btn"), ApplyCb(action="consent", value="yes")),
                                              (t(lang, "decline_btn"), ApplyCb(action="consent", value="no"))]]))]
    if app.step == "name":
        return [(t(lang, "ask_name"), None)]
    if app.step == "phone":
        keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t(lang, "phone_btn"), request_contact=True)]],
                                       resize_keyboard=True, one_time_keyboard=True)
        return [(t(lang, "ask_phone"), keyboard)]
    if app.step == "vacancy":
        return [(t(lang, "ask_vacancy"), await vacancy_buttons(session, lang))]
    if app.step == "experience":
        return [(t(lang, "ask_experience"), None)]
    if app.step == "resume":
        return [(t(lang, "ask_resume"), inline([[(t(lang, "skip_btn"), ApplyCb(action="skip", value="resume"))]]))]
    if app.step == "logic":
        index = A.current_logic(app)
        q = by_id(app.logic[index]["qid"])
        rows = [[(label, ApplyCb(action="logic", value=f"{index}-{i}"))]
                for i, label in enumerate(option_labels(q, lang))]
        return [(t(lang, "logic_q", n=index + 1, text=question_text(q, lang)), inline(rows))]
    if app.step == "job":
        index = A.current_job(app)
        return [(t(lang, "job_q", n=index + 4, text=app.job[index]["q"]), None)]
    return [status_reply(app)]


async def vacancy_buttons(session, lang: str):  # noqa: ANN001, ANN201
    rows = [[(title_for(v, lang), ApplyCb(action="vac", value=v.id))] for v in await list_open(session)]
    rows.append([(title_for(None, lang), ApplyCb(action="vac", value="general"))])
    return inline(rows)


def status_reply(app: Application) -> Reply:
    when = local(app.submitted_at).strftime("%d.%m.%Y") if app.submitted_at else ""
    return (t(app.language, f"status_{app.status}", date=when),
            inline([[(t(app.language, "apply_again_btn"), ApplyCb(action="again", value=""))],
                    [(t(app.language, "delete_btn"), ApplyCb(action="delete", value=""))]]))


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
            menu = main_menu(emp, await handles_work(session, emp))
            await message.answer(t(emp.language, "already_staff"), reply_markup=menu)
            return
        if await A.draft_for(session, tg) is None:
            done = await A.last_submitted(session, tg)
            if done is not None:
                await send(message, [status_reply(done)])
                return
        app, created = await A.start_or_resume(session, tg, command.args)
        app_id = app.id
    if created:
        logger.info("Application started from {}", command.args)
    await show_step(message, bot, app_id)


@router.callback_query(ApplyCb.filter(F.action == "again"))
async def apply_again(query: CallbackQuery, bot: Bot) -> None:
    await query.answer()
    tg = query.from_user.id
    async with session_scope() as session:
        emp = await staff_by_tg(session, tg)
        if emp is None:
            app, _ = await A.start_or_resume(session, tg, "apply")
            app_id = app.id
    if emp is not None:
        await query.message.answer(t(emp.language, "already_staff"))
        return
    await show_step(query.message, bot, app_id)


@router.message(CommandStart(magic=F.args.is_(None)), Applicant(submitted_too=True))
async def plain_start(message: Message, state: FSMContext, bot: Bot, app_id: str) -> None:
    await state.clear()
    await show_step(message, bot, app_id)


@router.message(Command("bekor", "cancel"), Applicant())
async def cancel(message: Message, app_id: str) -> None:
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        lang = app.language
    await message.answer(t(lang, "cancelled"), reply_markup=ReplyKeyboardRemove())


# --------------------------------------------------------------------------- buttons

async def _draft(session, query: CallbackQuery) -> Application | None:  # noqa: ANN001
    if await staff_by_tg(session, query.from_user.id) is not None:
        return None
    return await A.draft_for(session, query.from_user.id)


@router.callback_query(ApplyCb.filter(F.action == "delete"))
async def ask_deletion(query: CallbackQuery, bot: Bot) -> None:
    """The applicant asks for their data to be deleted. A person at HR decides; a legal hold stops it
    (spec 2.0 §10, T24)."""
    await query.answer()
    tg = query.from_user.id
    async with session_scope() as session:
        if await staff_by_tg(session, tg) is not None:
            return
        app = await A.draft_for(session, tg) or await A.last_submitted(session, tg)
        if app is None:
            return
        lang = app.language
        if app.status == "draft":  # nothing was submitted: the draft simply goes
            await session.delete(app)
            await query.message.answer(t(lang, "no_consent"))
            return
        fresh = await retention.request_deletion(session, app)
        held, number, app_id = bool(app.legal_hold), app.number, app.id
        people = await recipients(session) if fresh else []
    await query.message.answer(t(lang, "delete_hold" if held else "delete_requested" if fresh else "delete_waiting"))
    from hari.bot.handlers.hiring import deletion_buttons  # here: hiring imports the staff handlers

    for person in people:
        await notify(bot, person, f"{number}: номзод шахсий маълумотини ўчиришни сўради."
                     + (" Legal hold ўрнатилган — юрист кўриб чиқиши керак." if held else
                        " Қонуний сақлаш асоси бўлмаса ўчиринг; бўлса, legal hold ўрнатинг."),
                     None if held else deletion_buttons(app_id))


@router.callback_query(ApplyCb.filter(~F.action.in_({"again", "delete"})))
async def on_button(query: CallbackQuery, callback_data: ApplyCb, bot: Bot) -> None:
    action, value = callback_data.action, callback_data.value
    before: list[Reply] = []
    async with session_scope() as session:
        app = await _draft(session, query)
        if app is None:
            await query.answer()
            return
        lang, now = app.language, now_utc()
        if action == "lang" and app.step == "lang" and value in LANG_BUTTONS:
            app.language = value
            A.go(app, "consent", now)
        elif action == "consent" and app.step == "consent":
            if value != "yes":
                await session.delete(app)
                await query.answer()
                await query.message.answer(t(lang, "no_consent"))
                return
            app.consent_at = now
            A.go(app, "name", now)
        elif action == "vac" and app.step == "vacancy":
            try:
                await A.choose_vacancy(session, app, None if value == "general" else value)
                A.go(app, "experience", now)
            except A.ApplyError as exc:
                before = [(t(lang, str(exc)), None)]
        elif action == "skip" and app.step == "resume":
            A.go(app, "logic", now)
        elif action == "logic" and app.step == "logic":
            index, _, choice = value.partition("-")
            if not (index.isdigit() and choice.isdigit()) or not A.answer_logic(app, int(index), int(choice), now):
                await query.answer()
                return
            if A.current_logic(app) is None:
                A.go(app, "job", now)
        else:
            await query.answer()  # an old button from an earlier step
            return
        app_id = app.id
    await query.answer()
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:  # noqa: BLE001 — the message may be too old to edit; nothing depends on it
        pass
    if before:  # the vacancy closed or was already applied for: say so, then the list again
        await send(query.message, before)
    await show_step(query.message, bot, app_id)


# --------------------------------------------------------------------------- typed answers, files

@router.message(~F.text.startswith("/"), Applicant(submitted_too=True))  # commands pass through
async def on_message(message: Message, bot: Bot, app_id: str) -> None:
    async with session_scope() as session:
        app = await session.get(Application, app_id)
        if app.status != "draft":
            await send(message, [status_reply(app)])
            return
        lang, step, now = app.language, app.step, now_utc()
        text = (message.text or "").strip() if message.text and not message.text.startswith("/") else ""
        before: list[Reply] = []
        if step == "phone" and message.contact is not None:
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
        elif step not in TEXT_STEPS:
            await message.answer(t(lang, "press_button"))
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
            before = [(f"{t(lang, 'phone_btn')}: {app.phone}", ReplyKeyboardRemove())]
        elif step == "experience":
            A.go(app, "resume", now)
        elif step == "resume":
            A.go(app, "logic", now)
        elif step == "job" and A.current_job(app) is None:
            await A.submit(session, app, now)
        finished = app.status == "submitted"
    if finished:
        await message.answer(t(lang, "thanks", days=settings.apply_reply_days))
        await evaluate_and_report(bot, app_id)
        return
    await show_step(message, bot, app_id, before)
