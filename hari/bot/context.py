"""What every typed action gets: who wrote, how to reply, and the two questions HARI asks instead of
showing buttons — "do you confirm? (ҳа / йўқ)" and "which one? (number)"."""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from hari.bot import texts as T
from hari.bot.ui import NO_KEYBOARD
from hari.db.models import Employee
from hari.translit import cyrillic_to_latin


class Pending(StatesGroup):
    confirm = State()  # data["pending"] = {"action", "args"}: runs on «ҳа»
    pick = State()  # data["pending"] = {"action", "args", "key", "options"}: the typed number fills args[key]


@dataclass
class Ctx:
    message: Message
    bot: Bot
    state: FSMContext
    emp: Employee  # the verified employee who wrote (read-only copy; reload it to change it)
    works: bool = False  # accepts steps or handles requests
    manages: bool = False  # HR or owner
    text: str = ""
    file_id: str | None = None  # "photo:…" / "doc:…" sent with the message
    confirmed: bool = False  # the person already typed «ҳа» to this action
    _keyboard_cleared: bool = False

    @property
    def tg(self) -> int:
        return self.message.from_user.id

    async def say(self, text: str) -> None:
        # The first reply also takes away the old menu keyboard on phones that still show it.
        markup = None if self._keyboard_cleared else NO_KEYBOARD
        self._keyboard_cleared = True
        await self.message.answer(in_their_script(self.emp, self.text, text), reply_markup=markup)


def in_their_script(emp: Employee | None, said: str | None, text: str) -> str:
    """HARI's reply in the alphabet the person just wrote in: someone set to Cyrillic who writes in Latin
    gets Latin back, like a person would answer; otherwise their chosen script."""
    if emp is not None and emp.language == "uz_cyrl" and written_in_latin(said or ""):
        return cyrillic_to_latin(text)
    return T.say(emp, text)


def written_in_latin(text: str) -> bool:
    """The person typed this message in the Latin alphabet ("ishlayapsanmi?"). Short words like "ha" and
    slash commands say nothing about it, and a code like HR-2026-11-0003 inside Cyrillic text does not count."""
    if not text or text.startswith("/"):
        return False
    latin = sum(1 for ch in text if "a" <= ch.lower() <= "z")
    cyrillic = sum(1 for ch in text if "Ѐ" <= ch <= "ӿ")
    return latin >= 4 and latin > 3 * cyrillic


async def ask_confirm(ctx: Ctx, summary: str, action: str, args: dict) -> None:
    """Nothing changes yet: HARI says what it will do and waits for «ҳа»."""
    await ctx.state.set_state(Pending.confirm)
    await ctx.state.update_data(pending={"action": action, "args": args})
    from hari import voice  # here: voice needs the database, this module is imported early

    await ctx.say(await voice.natural(T.CONFIRM.format(summary=summary), await voice.known_names()))


async def ask_pick(ctx: Ctx, question: str, action: str, args: dict, key: str,
                   options: list[tuple[str, str]]) -> None:
    """Several items fit: list them numbered; the typed number (or words of one) fills `args[key]`.
    `options`: (value, label)."""
    if not options:
        await ctx.say(T.NOT_UNDERSTOOD_REF)
        return
    await ctx.state.set_state(Pending.pick)
    await ctx.state.update_data(pending={"action": action, "args": args, "key": key,
                                         "options": [[v, label] for v, label in options]})
    lines = [question] + [f"{i}. {label}" for i, (_, label) in enumerate(options, start=1)]
    lines.append("\nРақамини ёзинг (бекор қилиш: «бекор»).")
    await ctx.say("\n".join(lines))
