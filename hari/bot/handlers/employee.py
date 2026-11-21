"""What every verified employee can do by writing: plan, steps, documents, requests, profile, questions.

The functions named in `ACTIONS` are run by hari.bot.handlers.chat once it knows what a message asks.
The forms below (a proof, the text of a request, which kind of request) wait for the next message."""

from __future__ import annotations

from datetime import timedelta

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message
from sqlalchemy import select

from hari import agenda, profile, voice
from hari import cases as C
from hari import digest, memory, nonresponse
from hari import intent as I
from hari import onboarding as O
from hari.access import can_read_document
from hari.answer import answer_question, logged_source
from hari.guard import match_rule
from hari.bot import texts as T
from hari.bot.common import staff_by_tg
from hari.bot.context import Ctx, ask_confirm, ask_pick, in_their_script
from hari.bot.notifications import acceptor_label, tell_case_handler, tell_step_acceptor
from hari.bot.ui import notify
from hari.config import settings
from hari.db.models import Employee, KnowledgeChunk, KnowledgeDocument, OnboardingStep, as_utc
from hari.db.session import session_scope
from hari.translit import normalize
from hari.worktime import fmt, local, now_utc, today_local, work_days_between

router = Router(name="employee")

# Kinds an employee can open by writing ("question" is only offered after an unanswered question).
MENU_CASE_TYPES = ["leave", "schedule", "timesheet", "certificate", "data_fix", "training",
                   "pay_question", "legal", "complaint", "support", "resignation", "other"]
# Words that pick a kind from the typed list, besides its number and label.
CASE_WORDS = {
    "leave": ("таътил", "отпуск", "касал", "бўлмайман", "отгул", "больнич"),
    "schedule": ("жадвал", "масофавий", "график", "удалён", "удален"),
    "timesheet": ("табель", "давомат"),
    "certificate": ("маълумотнома", "справк"),
    "data_fix": ("профил", "хато маълумот"),
    "training": ("ўқиш", "тренинг", "обучени"),
    "pay_question": ("иш ҳақи", "маош", "зарплат"),
    "legal": ("ҳуқуқий", "шартнома", "юрид", "договор"),
    "complaint": ("шикоят", "жалоб"),
    "support": ("суҳбат", "қўллаб", "поддерж"),
    "resignation": ("бўшаш", "ариза ёзиб кет", "увольн"),
    "other": ("бошқа", "другое"),
}
ABOUT_WORDS = {
    "manager": ("бевосита раҳбарим", "раҳбарим", "раҳбар", "бошлиғим", "руководител", "начальник", "manager"),
    "hr": ("hr масъули", "hr", "ҳр", "кадр", "эйчар"),
    "owner": ("компания раҳбари", "эгаси", "директор", "раҳбарият", "владел", "owner"),
    "other": ("бошқа ходим", "бошқа", "ҳамкасб", "коллег", "другое"),
}


class AskProof(StatesGroup):
    waiting = State()


class NewCase(StatesGroup):
    kind = State()
    about = State()
    body = State()


# --------------------------------------------------------------------------- the plan and its steps

async def my_steps(session, emp: Employee) -> list[tuple[str, OnboardingStep]]:  # noqa: ANN001
    """The steps of this person's plan whose day has come and that are still open, by their number
    in the plan (the number «режам» shows)."""
    plan = await O.get_plan(session, emp.id)
    if plan is None:
        return []
    due = {s.id for s in O.due_open_steps(emp, plan, today_local())}
    return [(str(n), s) for n, s in enumerate(plan.steps, start=1) if s.id in due or s.status == "submitted"]


def steps_context(steps: list[tuple[str, OnboardingStep]]) -> list[dict]:
    return [{"ref": ref, "title": s.title, "kind": s.kind, "status": s.status} for ref, s in steps]


def _plan_text(emp: Employee, plan, today) -> str:  # noqa: ANN001
    done, total = O.progress(plan)
    due = {s.id for s in O.due_open_steps(emp, plan, today)}
    elapsed = work_days_between(emp, plan.started_on, today)
    lines = [f"Мослашув режаси — {done}/{total} қадам қабул қилинган", f"Бошланган: {plan.started_on:%d.%m.%Y}"]
    phase = None
    for n, s in enumerate(plan.steps, start=1):
        if s.phase != phase:
            phase = s.phase
            lines.append(f"\n{O.PHASE_LABELS.get(phase, phase)}")
        icon = T.STEP_ICONS.get(s.status, "•") if s.day_offset <= elapsed or s.status != "pending" else T.STEP_ICON_FUTURE
        lines.append(f"{icon} {n}. {s.title} — {T.KIND_LABELS[s.kind]}, қабул: {T.ACCEPTOR_LABELS[s.acceptor]}")
        if s.status == "returned" and s.return_reason:
            lines.append(f"   Сабаб: {s.return_reason}")
    open_steps = [(n, s) for n, s in enumerate(plan.steps, start=1) if s.id in due]
    if open_steps:
        lines.append("\nБугунги очиқ қадамлар:")
        for n, s in open_steps:
            if s.kind == "read":
                lines.append(f"• {n}. {s.title} — ўқиш: «{n} ни очинг», ўқиб бўлгач: «{n} ни ўқидим»")
            else:
                lines.append(f"• {n}. {s.title} — бажаргач ёзинг: «{n} ни бажардим: нима қилдингиз» "
                             "(фото ёки файл ҳам бўлади)")
    return "\n".join(lines)


async def plan(ctx: Ctx, args: dict) -> None:
    await ctx.state.clear()
    async with session_scope() as session:
        found = await O.get_plan(session, ctx.emp.id)
        if found is None:
            await ctx.say("Сизга ҳали мослашув режаси бириктирилмаган. Керак бўлса, «мурожаат» деб ёзиб, "
                          "HRдан сўранг.")
            return
        text = _plan_text(ctx.emp, found, today_local())
    await ctx.say(text)


def _match_step(ref: object, steps: list[tuple[str, OnboardingStep]], text: str = "") -> OnboardingStep | None:
    """A step by its number ("2", "2-қадам") or by words of its title."""
    if isinstance(ref, int):
        ref = str(ref)
    if isinstance(ref, str) and ref.strip():
        digits = "".join(ch for ch in ref if ch.isdigit())
        for r, s in steps:
            if digits and r == digits:
                return s
    words = normalize((text or (ref if isinstance(ref, str) else "")).lower())
    hits = [s for _, s in steps if normalize(s.title.lower()) and normalize(s.title.lower()) in words]
    return hits[0] if len(hits) == 1 else None


async def _pick_step(ctx: Ctx, args: dict, action: str, kinds: set[str], question: str) -> OnboardingStep | None:
    """Resolve args["step"] to one of this person's open steps of these kinds; asks which when unclear."""
    async with session_scope() as session:
        steps = [(r, s) for r, s in await my_steps(session, ctx.emp) if s.kind in kinds]
        if args.get("step_id"):
            step = await session.get(OnboardingStep, args["step_id"])
            if step is not None and any(s.id == step.id for _, s in steps):
                return step
        step = _match_step(args.get("step"), steps, ctx.text if ctx.text and not args.get("step") else "")
    if step is None and len(steps) == 1:
        step = steps[0][1]
    if step is not None:
        return step
    if not steps:
        await ctx.say("Ҳозир бундай очиқ қадамингиз йўқ. Режани кўриш учун «режам» деб ёзинг.")
        return None
    await ask_pick(ctx, question, action, args, "step", [(r, s.title) for r, s in steps])
    return None


async def read_doc(ctx: Ctx, args: dict) -> None:
    step = await _pick_step(ctx, args, "read_doc", {"read"}, "Қайси қадамнинг ҳужжатини очай?")
    if step is None:
        return
    async with session_scope() as session:
        doc = await session.get(KnowledgeDocument, step.doc_id) if step.doc_id else None
        # Opening checks the right again; an unknown and a forbidden document look the same (spec §22).
        if doc is None or not can_read_document(ctx.emp, doc, today_local()):
            await ctx.say("Ҳужжат ҳозир очиқ эмас ёки ҳали тасдиқланмаган.")
            return
        chunks = (await session.execute(
            select(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.doc_id, KnowledgeChunk.doc_version == doc.version)
            .order_by(KnowledgeChunk.position)
        )).scalars().all()
    body = "\n\n".join(f"{c.section}\n{c.text}" if c.section != doc.title else c.text for c in chunks)
    await ctx.say(f"{doc.title}, {doc.version}-версия\n\n{body}"[:3600] + "\n\nЎқиб бўлгач, «ўқидим» деб ёзинг.")


async def step_read(ctx: Ctx, args: dict) -> None:
    step = await _pick_step(ctx, args, "step_read", {"read"}, "Қайси қадам билан танишдингиз?")
    if step is None:
        return
    async with session_scope() as session:
        emp = await staff_by_tg(session, ctx.tg)
        try:
            step = await O.acknowledge(session, emp, step.id)
        except O.StepError:
            await ctx.say("Бу қадам аллақачон ёпилган ёки сизга тегишли эмас.")
            return
        found = await O.get_plan(session, emp.id)
        finished = found is not None and found.status == "done"
        upcoming = None if finished else await agenda.next_step(session, emp)
    await ctx.say(f"«{step.title}» — танишганингиз қайд этилди." + (f"\n\n{upcoming}" if upcoming else ""))
    if finished:
        await ctx.say(T.PLAN_DONE)


async def step_done(ctx: Ctx, args: dict) -> None:
    step = await _pick_step(ctx, args, "step_done", {"task", "meeting"}, "Қайси қадамни бажардингиз?")
    if step is None:
        return
    proof, file_id = args.get("proof"), args.get("file_id") or ctx.file_id
    if not proof and not file_id:
        await ctx.state.set_state(AskProof.waiting)
        await ctx.state.update_data(step_id=step.id)
        await ctx.say(T.ASK_PROOF.format(title=step.title, acceptor=acceptor_label(step)))
        return
    if not ctx.confirmed:
        what = f"«{proof}»" if proof else "юборган файлингиз"
        await ask_confirm(ctx, f"«{step.title}» натижаси сифатида {what}ни {acceptor_label(step)}га юбораман.",
                          "step_done", {"step_id": step.id, "proof": proof, "file_id": file_id})
        return
    await _submit_proof(ctx.message, ctx.bot, ctx.tg, step.id, proof, file_id)


async def _submit_proof(message: Message, bot: Bot, tg: int, step_id: str, text: str | None,
                        file_id: str | None) -> None:
    async with session_scope() as session:
        emp = await staff_by_tg(session, tg)
        if emp is None:
            return
        try:
            step, owner = await O.submit(session, emp, step_id, text, file_id)
        except O.StepError as exc:
            await message.answer(T.say(emp, "Натижа сақланмади: " + (
                "матн ёки файл юборинг." if str(exc) == "no_proof" else "бу қадам сизга тегишли эмас ёки ёпилган.")))
            return
        acceptor = await O.acceptor_for(session, step, owner)
    delivered = acceptor is not None and await tell_step_acceptor(bot, acceptor, owner, step)
    if acceptor is not None:  # the acceptor now owes an answer; whether the card arrived is recorded as is
        async with session_scope() as session:
            await nonresponse.track(session, acceptor, "step", step.id,
                                    f"Мослашув: {owner.full_name} — «{step.title}» қабул кутмоқда", bool(delivered))
    await message.answer(in_their_script(emp, message.text or message.caption,
                                         T.PROOF_SENT.format(acceptor=acceptor_label(step)) if delivered
                                         else T.PROOF_NO_ACCEPTOR))


@router.message(AskProof.waiting, F.text | F.photo | F.document)
async def receive_proof(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    text = message.text or message.caption
    await state.clear()
    if message.text and I.is_no(message.text):
        await message.answer(T.CANCELLED)
        return
    file_id = None
    if message.photo:
        file_id = f"photo:{message.photo[-1].file_id}"
    elif message.document:
        file_id = f"doc:{message.document.file_id}"
    await _submit_proof(message, bot, message.from_user.id, data.get("step_id", ""), text, file_id)


async def file_sent(ctx: Ctx) -> None:
    """A photo or file with no words: the result of the one task step waiting, or the person says which."""
    async with session_scope() as session:
        steps = [(r, s) for r, s in await my_steps(session, ctx.emp)
                 if s.kind in ("task", "meeting") and s.status in ("pending", "returned")]
    if not steps:
        await ctx.say("Файлни олдим, лекин ҳозир натижа кутаётган қадамингиз йўқ. Нима учун юборганингизни ёзинг.")
        return
    await step_done(ctx, {"step": steps[0][0] if len(steps) == 1 else None, "file_id": ctx.file_id})


# --------------------------------------------------------------------------- requests

def _case_options() -> list[tuple[str, tuple[str, ...]]]:
    return [(s, (C.CASE_TYPES[s].label, *CASE_WORDS.get(s, ()))) for s in MENU_CASE_TYPES]


def _case_list() -> str:
    return "\n".join(f"{i}. {C.CASE_TYPES[s].label}" for i, s in enumerate(MENU_CASE_TYPES, start=1))


def _about_list() -> str:
    return "\n".join(f"{i}. {label}" for i, label in enumerate(T.ABOUT_LABELS.values(), start=1))


async def new_case(ctx: Ctx, args: dict) -> None:
    kind, about, text = args.get("type"), args.get("about"), args.get("text")
    if kind not in MENU_CASE_TYPES:
        await ctx.state.set_state(NewCase.kind)
        await ctx.say(f"{T.CASE_PICK}\n\n{_case_list()}\n\nБекор қилиш: «бекор».")
        return
    ctype = C.CASE_TYPES[kind]
    if kind == "complaint" and about not in T.ABOUT_LABELS:
        # First who it is about, so the request can never be routed to that person (spec 2.0 §2, T16).
        await ctx.state.set_state(NewCase.about)
        await ctx.state.update_data(case_type=kind, body=text)
        await ctx.say(f"{T.COMPLAINT_ABOUT}\n\n{_about_list()}")
        return
    if not text:
        await ctx.state.set_state(NewCase.body)
        await ctx.state.update_data(case_type=kind, about=about)
        body = T.CASE_ASK_BODY.format(label=ctype.label)
        if kind == "resignation":
            body += "\n\n" + T.RESIGNATION_NOTE
        elif kind == "complaint":
            body += "\n\n" + T.CASE_COMPLAINT_NOTE
        await ctx.say(body)
        return
    if not ctx.confirmed:
        await ask_confirm(ctx, f"«{ctype.label}» мурожаатини очаман. Матни: «{text}».",
                          "new_case", {"type": kind, "about": about, "text": text})
        return
    await open_and_tell(ctx.message, ctx.bot, ctx.tg, kind, text, about)


@router.message(NewCase.kind, F.text)
async def case_kind(message: Message, state: FSMContext, bot: Bot) -> None:
    await state.clear()
    if I.is_no(message.text):
        await message.answer(T.CANCELLED)
        return
    kind = I.choice(message.text, _case_options())
    from hari.bot.handlers.chat import ctx_for  # here: chat imports this module

    ctx = await ctx_for(message, state, bot)
    if ctx is None:
        return
    if kind is not None and len(message.text.split()) <= 3:  # "1", "таътил": the kind, the text comes next
        await new_case(ctx, {"type": kind})
        return
    # A description instead of a number: the kind comes from it, and the words are the request. A topic with
    # a fixed safety rule (pay, complaint, …) takes the rule's kind and never goes to the AI.
    rule = match_rule(message.text) if kind is None else None
    if rule is not None:
        kind = rule.case_type if rule.case_type in MENU_CASE_TYPES else None
    elif kind is None and not settings.hari_paused:
        async with session_scope() as session:
            people = [(c, n) for c, n in (await session.execute(select(Employee.code, Employee.full_name))).all()]
        found = await I.route(message.text, ["new_case"], {}, people)
        if found is not None and found.args.get("type") in MENU_CASE_TYPES:
            kind = found.args["type"]
    await new_case(ctx, {"type": kind or "other", "about": None, "text": " ".join(message.text.split())})


@router.message(NewCase.about, F.text)
async def complaint_about(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    if I.is_no(message.text):
        await message.answer(T.CANCELLED)
        return
    about = I.choice(message.text, list(ABOUT_WORDS.items()))
    from hari.bot.handlers.chat import ctx_for

    ctx = await ctx_for(message, state, bot)
    if ctx is None:
        return
    if about is None:
        await state.set_state(NewCase.about)
        await state.update_data(case_type="complaint", body=data.get("body"))
        await ctx.say(f"Тушунмадим. {T.COMPLAINT_ABOUT}\n\n{_about_list()}")
        return
    if data.get("body"):  # the complaint was already written: open it now (the person chose who it is about)
        await open_and_tell(message, bot, message.from_user.id, "complaint", data["body"], about)
        return
    await new_case(ctx, {"type": "complaint", "about": about})


@router.message(NewCase.body, F.text)
async def case_body(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    if I.is_no(message.text):
        await message.answer(T.CANCELLED)
        return
    await open_and_tell(message, bot, message.from_user.id, data.get("case_type", "other"), message.text,
                        data.get("about"))


async def open_and_tell(message: Message, bot: Bot, telegram_user_id: int, case_type: str, body: str,
                        about_target: str | None = None, dedupe: str | None = None,
                        quiet: bool = False) -> None:
    """Open the request, hand it to its handler and tell the requester who will see it.
    `quiet`: the requester was already told what happens (an exit-intent signal)."""
    alerts: list[tuple[Employee, str]] = []
    async with session_scope() as session:
        emp = await staff_by_tg(session, telegram_user_id)
        if emp is None:
            return
        about, about_role = None, None
        if about_target == "manager" and emp.manager_id:
            about = await session.get(Employee, emp.manager_id)
        elif about_target in ("hr", "owner"):
            about_role = about_target
        case, created = await C.open_case(session, emp, case_type, body, about=about, dedupe=dedupe,
                                          about_role=about_role)
        assignee = await session.get(Employee, case.assignee_id) if case.assignee_id else None
        independent = await C.independent_recipient(session)
        if created:
            delivered = await tell_case_handler(bot, session, case)
            if assignee is not None:
                await nonresponse.track(session, assignee, "case", case.id, nonresponse.case_title(case),
                                        delivered, now_utc(), about_employee_id=case.about_employee_id)
            else:  # nobody holds the role: someone allowed must still see it, and the owner's digest lists it
                for person in await _any_hr(session):
                    if C.can_handle_case(person, None, case.about_employee_id, case.about_role):
                        await notify(bot, person, f"⚠️ {emp.full_name}нинг «{case.title}» мурожаати учун масъул "
                                                  "топилмади. Кўриш учун «иш навбати» деб ёзинг.")
                await digest.add(session, f"unassigned:{case.id}", "unassigned", priority=20,
                                 fact=f"«{case.title}» мурожаати учун масъул топилмади.",
                                 impact="Мурожаат эгасиз қолди: биринчи жавоб муддати ўтиши мумкин.",
                                 responsible="тайинланмаган", due=fmt(case.first_response_due),
                                 about_employee_id=case.about_employee_id, about_role=case.about_role)
            alerts = await C.continuity_signal(session, case, emp)
    for owner, text in alerts:
        await notify(bot, owner, text)
    if quiet:
        return
    who = f"{assignee.full_name} ({assignee.position})" if assignee else T.CASE_UNASSIGNED
    text = T.CASE_OPENED.format(label=case.title, who=who, due=fmt(case.first_response_due))
    if (about_role or (about is not None and about.role in ("hr", "owner"))) and independent is None:
        text += "\n\n" + T.COMPLAINT_NO_INDEPENDENT
    await message.answer(in_their_script(emp, message.text, text))


async def _any_hr(session) -> list[Employee]:  # noqa: ANN001
    rows = (await session.execute(select(Employee).where(Employee.role.in_(("hr", "owner")),
                                                         Employee.status == "active"))).scalars().all()
    return [r for r in rows if r.telegram_user_id]


async def take_offer(ctx: Ctx, slug: str, question: str) -> None:
    """The person answered «ҳа» to the request HARI offered after its answer."""
    await ctx.state.update_data(last_question=None, offer=None)
    if slug == "complaint":  # ask who it is about before it is routed anywhere (T16)
        await ctx.state.set_state(NewCase.about)
        await ctx.state.update_data(case_type="complaint", body=question)
        await ctx.say(f"{T.COMPLAINT_ABOUT}\n\n{_about_list()}")
        return
    await open_and_tell(ctx.message, ctx.bot, ctx.tg, slug, question)


STATUS_WORDS = {"submitted": "юборилди", "accepted": "кўриб чиқилмоқда", "in_review": "кўриб чиқилмоқда",
                "decided": "жавоб берилди, иш очиқ", "closed": "ёпилди", "rejected": "рад этилди"}


async def my_cases(ctx: Ctx, args: dict) -> None:
    await ctx.state.clear()
    async with session_scope() as session:
        rows = await C.mine(session, ctx.emp)
    if not rows:
        await ctx.say("Сизда ҳали мурожаат йўқ. Очиш учун нима кераклигини ёзинг, масалан: «маълумотнома керак».")
        return
    lines = ["Мурожаатларингиз:"]
    for c in rows:
        lines.append(f"\n{c.title} ({local(as_utc(c.created_at)):%d.%m})\nҲолат: {STATUS_WORDS.get(c.status, c.status)}")
        if c.decision:
            lines.append(f"Жавоб: {c.decision}")
    await ctx.say("\n".join(lines))


async def case_show(ctx: Ctx, args: dict) -> None:
    """A request by number: its handler sees the card, its requester the status; anyone else nothing."""
    from hari.bot.notifications import case_card

    async with session_scope() as session:
        case = await C.get_by_number(session, args.get("number") or "")
        if case is None:
            await ctx.say("Бундай мурожаат топилмади.")
            return
        if C.can_handle_case(ctx.emp, case.assignee_id, case.about_employee_id, case.about_role) \
                and case.requester_id != ctx.emp.id:
            requester = await session.get(Employee, case.requester_id)
            about = await session.get(Employee, case.about_employee_id) if case.about_employee_id else None
            text = (f"{case_card(case, requester, about)}\nҲолат: {STATUS_WORDS.get(case.status, case.status)}"
                    + (f"\nЖавоб: {case.decision}" if case.decision else "")
                    + "\n\nЖавоб бериш: «жавоб: …»")
        elif case.requester_id == ctx.emp.id:
            text = (f"{case.title}\nҲолат: {STATUS_WORDS.get(case.status, case.status)}"
                    + (f"\nЖавоб: {case.decision}" if case.decision else ""))
        else:
            text = "Бундай мурожаат топилмади."  # someone else's request looks like a missing one
    await ctx.say(text)


# --------------------------------------------------------------------------- profile

LANG_WORDS = {"uz_cyrl": ("кирилл", "кириллда", "ўзбекча кирилл", "крилл"),
              "uz_latn": ("лотин", "лотинда", "lotin", "лотиница", "латин"),
              "ru": ("рус", "русча", "русский", "по русски", "по-русски", "rus")}


ROLE_WORDS = {"employee": "ходим", "manager": "раҳбар", "hr": "HR", "accountant": "бухгалтер", "lawyer": "юрист",
              "it_admin": "IT админ", "owner": "компания эгаси"}
DAY_NAMES = ["Ду", "Се", "Чо", "Па", "Жу", "Ша", "Як"]


async def my_profile(ctx: Ctx, args: dict) -> None:
    """The person's own card — the same shape for every role; each person sees only their own."""
    await ctx.state.clear()
    async with session_scope() as session:
        emp = await session.get(Employee, ctx.emp.id)
        manager = await session.get(Employee, emp.manager_id) if emp.manager_id else None
        mentor = await session.get(Employee, emp.mentor_id) if emp.mentor_id else None
        found = await O.get_plan(session, emp.id)
        progress = None
        if found is not None:
            done, total = O.progress(found)
            progress = "тугаган" if found.status == "done" else f"{done}/{total} қадам қабул қилинган"
        open_cases = sum(1 for c in await C.mine(session, emp) if c.status not in ("closed", "rejected"))
    lang = {"uz_cyrl": "ўзбек (кирилл)", "uz_latn": "ўзбек (лотин)", "ru": "рус"}[emp.language]
    days = ", ".join(DAY_NAMES[int(d)] for d in emp.work_days.split(",") if d.strip().isdigit() and int(d) < 7)
    lines = [
        emp.full_name,
        f"{emp.position}, {emp.department}" + (f" — {ROLE_WORDS[emp.role]}" if emp.role != "employee" else ""),
        "",
        f"Раҳбарингиз: {manager.full_name if manager else '—'}",
        f"Мураббийингиз: {mentor.full_name if mentor else '—'}",
        f"Ишга келган сана: {emp.start_date:%d.%m.%Y}" if emp.start_date else "Ишга келган сана: —",
        f"Иш вақти: {days or '—'}, {emp.work_start_hour}:00–{emp.work_end_hour}:00",
        f"Мен сизга {lang}да ёзаман",
    ]
    if progress:
        lines.append(f"Мослашув: {progress}")
    if open_cases:
        lines.append(f"Очиқ мурожаатларингиз: {open_cases} та")
    await ctx.say("\n".join(lines))


async def set_language(ctx: Ctx, args: dict) -> None:
    lang = args.get("lang")
    if lang not in I.LANGS:
        lang = I.choice(ctx.text, list(LANG_WORDS.items()))
    if lang not in I.LANGS:
        await ctx.say("Қайси тилда ёзай: кирилл, лотин ёки рус?")
        return
    async with session_scope() as session:
        emp = await session.get(Employee, ctx.emp.id)
        await profile.apply(session, emp, emp.id, [["language", lang]], how="language")
    ctx.emp.language = lang
    await ctx.say("Тил сақланди. Бундан буён шу тилда ёзаман.")


async def forget(ctx: Ctx, args: dict) -> None:
    """The person erases what HARI remembers of their own chat (requests they opened stay)."""
    await ctx.state.update_data(offer=None, last_question=None)
    async with session_scope() as session:
        emp = await session.get(Employee, ctx.emp.id)
        await memory.forget(session, emp)
    await ctx.say(T.MEMORY_FORGOTTEN)


async def help_(ctx: Ctx, args: dict) -> None:
    await ctx.say(await voice.natural(T.help_text(ctx.emp, ctx.works, ctx.manages)))


# --------------------------------------------------------------------------- questions

async def source(ctx: Ctx, args: dict) -> None:
    """Where the last answer came from. Answers go out without a source line; this is how to see it."""
    line = (await ctx.state.get_data()).get("last_source")
    if not line:  # after a restart the chat's memory is gone: the answer log still knows
        async with session_scope() as session:
            line = await logged_source(session, ctx.emp)
    await ctx.say(line or "Охирги жавобим бирор ҳужжатга таянмаган эди — у оддий суҳбат эди.")


FOLLOW_UP_MINUTES = 10  # a short reply joins the previous message in a request only within this time


async def question(ctx: Ctx, args: dict) -> None:
    text = ctx.text
    async with session_scope() as session:
        emp = await session.get(Employee, ctx.emp.id)
        await ctx.bot.send_chat_action(ctx.message.chat.id, ChatAction.TYPING)
        # Only this person's own earlier messages: the employee comes from their verified Telegram account.
        turns = await memory.recent(session, emp, now_utc())
        result = await answer_question(session, emp, text, today_local(), history=turns)
        if result.outcome not in ("rule", "paused"):
            # A topic with a fixed safety reply is not kept: it must never reach the AI later either.
            await memory.remember(session, emp, text, result.text, now_utc())
    # What a request opened from here will say: the message, and the one just before it when this is a
    # short follow-up in the same conversation ("I told you, my head hurts" means little to HR on its own).
    # Never an older message: the person did not choose to send last week's words to their manager.
    body = text
    if (result.outcome != "rule" and turns and len(text.split()) <= 6
            and now_utc() - turns[-1][3] <= timedelta(minutes=FOLLOW_UP_MINUTES)):
        body = f"{turns[-1][0]}\n{text}"
    await ctx.state.update_data(offer=result.offer_case, last_question=body if result.offer_case else None)
    if result.source:  # not shown with the answer: said when they ask "манбаси?"
        await ctx.state.update_data(last_source=result.source)
    if result.auto_case and not settings.hari_paused:
        # A stated wish to leave: HR gets the fact in HARI's own words, once a day at most — never the
        # message itself (spec 2.0 §18, T11). The reply above already told the person who sees it.
        await open_and_tell(ctx.message, ctx.bot, ctx.tg, result.auto_case, C.EXIT_INTENT_BODY,
                            dedupe=f"{result.auto_case}:{emp.id}:{today_local()}", quiet=True)
    reply = result.text
    if result.offer_case and "«ҳа»" not in reply:  # the reply may already say it, in its own words
        reply += "\n\n" + (T.OFFER_QUESTION_HINT if result.offer_case == "question" else T.OFFER_CASE_HINT)
    # Model text is sent as plain text (no parse mode): nothing in it becomes a link or markup.
    await ctx.say(reply)


ACTIONS = {
    "question": question, "help": help_, "plan": plan, "read_doc": read_doc, "step_read": step_read,
    "step_done": step_done, "new_case": new_case, "my_cases": my_cases, "case_show": case_show,
    "my_profile": my_profile, "set_language": set_language, "forget": forget, "source": source,
}
