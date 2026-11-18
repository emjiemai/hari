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
        await self.message.answer(T.say(self.emp, text), reply_markup=markup)


async def ask_confirm(ctx: Ctx, summary: str, action: str, args: dict) -> None:
    """Nothing changes yet: HARI says what it will do and waits for «ҳа»."""
    await ctx.state.set_state(Pending.confirm)
    await ctx.state.update_data(pending={"action": action, "args": args})
    await ctx.say(T.CONFIRM.format(summary=summary))


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
