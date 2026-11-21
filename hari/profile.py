"""A person's card, changed by saying it, with every change kept.

«Исмимни Эшонов Шахзод қил, лавозимим Директор», «Анварнинг раҳбари Улуғ бўлсин, иш вақти 9-18»,
«поменяй мою должность на Директор»: `parse` finds which fields are named and the new value of each;
`resolve` checks a value and turns it into what is stored (a person's name into their card, a date into a
date…); `apply` changes the card and writes one `ProfileChange` row per field — what it was, what it became,
who changed it and when. Nothing in that table is ever updated or deleted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit
from hari.db.models import Employee, ProfileChange
from hari.translit import normalize
from hari.worktime import today_local

LABELS = {"name": "исм", "position": "лавозим", "department": "бўлим", "manager": "раҳбар",
          "mentor": "мураббий", "start_date": "ишга келган сана", "hours": "иш вақти", "days": "иш кунлари",
          "language": "тил", "role": "роль"}
KEYS = tuple(k for k in LABELS if k != "role")  # what can be changed by writing (a role needs the owner's approval)

LANG_NAMES = {"uz_cyrl": "ўзбек (кирилл)", "uz_latn": "ўзбек (лотин)", "ru": "рус"}
LANG_WORDS = {"uz_cyrl": ("кирилл", "крилл", "кирил"), "uz_latn": ("лотин", "латин", "латиница"),
              "ru": ("рус", "русский", "русча", "по русски")}
DAY_SHORT = ["Ду", "Се", "Чо", "Па", "Жу", "Ша", "Як"]
_DAY_WORDS = [("ду", "душанба", "пн", "понедельник"), ("се", "сешанба", "вт", "вторник"),
              ("чо", "чоршанба", "ср", "среда"), ("па", "пайшанба", "чт", "четверг"), ("жу", "жума", "пт", "пятница"),
              ("ша", "шанба", "сб", "суббота"), ("як", "якшанба", "вс", "воскресенье")]
_MONTHS = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")
_NOBODY = {"йўқ", "йук", "нет", "ҳеч ким", "никто", "керакмас", "олиб ташла", "убери"}

# --------------------------------------------------------------------------- reading the message

# How each field is named: a stem and the endings it may carry («лавозим», «лавозими», «лавозимимни» …).
_ENDINGS = ("", "и", "ини", "им", "имни", "имга", "ига", "ни", "га", "имиз", "ь", "я", "а", "е", "ем", "ом", "у",
            "ю", "ью", "лари", "ларим", "ларини", "ларимни")
_STEMS = {
    "name": ("исм", "фиш", "фио", "фамилия", "имя", "имени"),
    "position": ("лавозим", "должност"),
    "department": ("бўлим", "отдел"),
    "manager": ("руководител", "начальник"),
    "mentor": ("мураббий", "наставник", "ментор"),
    "hours": ("график",),
}
# «раҳбар» alone is a role («Анварни раҳбар қил»); «раҳбари», «раҳбарим» is the person they report to.
_MANAGER = {"раҳбари", "раҳбарим", "раҳбарини", "раҳбаримни", "раҳбарига", "раҳбаримиз"}
_LANG = {"тил", "тили", "тилим", "тилни", "тилини", "тилимни", "язык", "языка", "языке"}
_PAIRS = {
    "start_date": ("ишга келган", "ишга кирган", "иш бошлаган", "ишни бошлаган", "бошлаш сана", "дата начала",
                   "дата выхода", "келган сана"),
    "hours": ("иш вақт", "иш соат", "рабочее время", "часы работы", "время работы"),
    "days": ("иш кун", "рабочие дни", "рабочий день", "рабочих дней", "дни работы"),
}
_LEAD = {"на", "энди", "как", "қилиб", "қилиб қўй", "это", "бу", "ўрнига"}
_TAIL = {"ва", "ҳам", "и", "бўлсин", "бўлади", "қил", "қилинг", "қилиб", "қўй", "қўйинг", "ўзгартир", "ўзгартиринг",
         "ўзгартириб", "алмаштир", "алмаштиринг", "ёз", "ёзинг", "деб", "сифатида", "пусть", "будет", "сделай",
         "поменяй", "измени", "поставь", "смени", "илтимос", "пожалуйста", "керак", "этиб", "белгила", "белгиланг",
         "янгила", "янгиланг", "тайинла", "тайинланг", "қилсин", "қилинсин", "бер", "беринг"}
_TURN = ("ўзгартир", "алмаштир")  # «Директорга ўзгартир»: the «-га» belongs to the verb, not the value
_MINE = {"менинг", "мени", "ўзимнинг", "ўзимни", "мой", "моя", "мою", "мои", "моё", "мое", "мне", "меня"}


def _n(token: str) -> str:
    return " ".join(normalize(token.lower()).split())


def _stem_of(word: str) -> tuple[str | None, bool]:
    """(field, said of oneself) for one word that names a field, else (None, False)."""
    if word in _MANAGER:
        return "manager", word.startswith("раҳбарим")
    if word in _LANG:
        return "language", word.startswith("тилим")
    for key, stems in _STEMS.items():
        for stem in stems:
            if word.startswith(stem) and word[len(stem):] in _ENDINGS:
                return key, word[len(stem):].startswith("им")
    return None, False


@dataclass
class Parsed:
    changes: list[dict] = field(default_factory=list)  # [{"field": key, "value": text or None}]
    mine: bool = False  # the person speaks of their own card («исмим», «мой отдел»)
    who: str = ""  # the words that are not a field or a value: whose card it is («Анварнинг»)


def parse(raw: str) -> Parsed:
    """Which fields the message names and the new value of each, in the person's own words."""
    tokens = (raw or "").split()
    norms = [_n(t) for t in tokens]
    found: list[tuple[str, int, int, bool]] = []  # (field, first token, after the last token, mine)
    def starts(i: int, mine: bool) -> bool:
        """A later field starts a new part only after a break («…, бўлими …», «… ва бўлими …») or when it is
        said of oneself; otherwise it belongs to the value before it («лавозими: Сотув бўлими бошлиғи»)."""
        if not found:
            return True
        if found[-1][2] == i:  # right after another field's name: it is that field's value
            return False
        before = tokens[i - 1]
        return (mine or tokens[i].endswith(":") or before.endswith((",", ";", ".", ":"))
                or norms[i - 1] in _TAIL or norms[i - 1].endswith("нинг"))

    i = 0
    while i < len(tokens):
        pair = f"{norms[i]} {norms[i + 1]}" if i + 1 < len(tokens) else ""
        key = next((k for k, phrases in _PAIRS.items() if any(pair.startswith(p) for p in phrases)), None)
        if key:
            mine = norms[i + 1].endswith(("им", "имни", "имиз")) or norms[i + 1].startswith(("кунларим", "вақтим"))
            end = i + 2
            if key == "start_date" and end < len(tokens) and norms[end].startswith(("сана", "дата")):
                mine, end = mine or norms[end].startswith("санам"), end + 1  # «ишга келган санам»
            if starts(i, mine):
                found.append((key, i, end, mine))
                i = end
                continue
        key, mine = _stem_of(norms[i])
        if key and starts(i, mine):
            found.append((key, i, i + 1, mine))
        i += 1
    out = Parsed(mine=any(m for *_, m in found) or any(n in _MINE for n in norms))
    if not found:
        return out
    asking = raw.strip().endswith("?")  # «лавозимим нима?» asks, it does not change anything
    who = tokens[:found[0][1]]
    for j, (key, start, end, _) in enumerate(found):
        stop = found[j + 1][1] if j + 1 < len(found) else len(tokens)
        value, verb = list(tokens[end:stop]), False
        while value and (_n(value[0]) in _LEAD or not _n(value[0])):
            value.pop(0)
        while value and (_n(value[-1]) in _TAIL or not _n(value[-1]) or _n(value[-1]).startswith(_TURN)):
            verb = verb or _n(value[-1]).startswith(_TURN)
            value.pop()
        if verb and value and _n(value[-1]).endswith("га") and len(_n(value[-1])) > 4:
            value[-1] = value[-1][:-2]
        text = " ".join(value).strip(" «»\"'.,;:—-")
        out.changes.append({"field": key, "value": None if asking else (text or None)})
    out.who = " ".join(w for w in who if _n(w) not in _MINE | _TAIL)
    return out


def says_change(raw: str) -> bool:
    """A field of one's own card with a new value next to it: «исмимни … қил», «лавозимим: …»."""
    parsed = parse(raw)
    words = _n(raw)
    verb = ":" in raw or any(re.search(rf"(?<!\w){t}", words) for t in
                             ("қил", "бўлсин", "ўзгартир", "алмаштир", "тузат", "ёз", "поменяй", "измени",
                              "смени", "исправ", "поставь", "сделай", "янгила"))
    return parsed.mine and verb and any(c["value"] for c in parsed.changes)


# --------------------------------------------------------------------------- checking a value

class BadValue(Exception):  # noqa: N801 — a value HARI cannot store, with what to tell the person
    pass


async def _person(session: AsyncSession, text: str, emp: Employee) -> Employee | None:
    from hari import intent as I  # noqa: N812 — here: intent imports this module

    words = _n(text)
    if words in _NOBODY:
        return None
    code = I.employee_code(text)
    people = list((await session.execute(select(Employee).where(Employee.status != "archived"))).scalars())
    if code:
        fits = [p for p in people if p.code == code]
    else:
        fits = [p for p in people if any(len(w) >= 3 and _n(w) and re.search(rf"(?<!\w){re.escape(_n(w))}", words)
                                         for w in p.full_name.split())]
        best = [p for p in fits if all(_n(w) in _n(p.full_name) for w in text.split() if len(_n(w)) >= 3)]
        fits = best or fits
    if not fits:
        raise BadValue(f"«{text}» деган ходимни топмадим.")
    if len(fits) > 1:
        raise BadValue(f"«{text}» — бир нечта ходим: " + ", ".join(p.full_name for p in fits[:5])
                          + ". Тўлиқроқ ёзинг.")
    if fits[0].id == emp.id:
        raise BadValue("Ўзи ўзига раҳбар ёки мураббий бўла олмайди.")
    return fits[0]


def _date(text: str) -> date:
    words, today = _n(text), today_local()
    if words in ("бугун", "сегодня"):
        return today
    if words in ("кеча", "вчера"):
        return today - timedelta(days=1)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    try:
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.search(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", text)
        if m:
            year = int(m[3]) if m[3] else today.year
            return date(year + 2000 if year < 100 else year, int(m[2]), int(m[1]))
        m = re.search(r"(\d{1,2})\s*([а-яўқғҳ]+)\s*(\d{4})?", words)
        if m and m[2][:3] in _MONTHS:
            return date(int(m[3]) if m[3] else today.year, _MONTHS.index(m[2][:3]) + 1, int(m[1]))
    except ValueError:
        pass
    raise BadValue(f"«{text}» — санани тушунмадим. Масалан: 10.11.2026.")


def _hours(text: str) -> tuple[int, int]:
    m = re.search(r"(\d{1,2})(?:[:.]\d{2})?\s*(?:-|–|—|дан|dan|до|to|гача)\s*(\d{1,2})", text, re.IGNORECASE)
    if m and 0 <= int(m[1]) < int(m[2]) <= 24:
        return int(m[1]), int(m[2])
    raise BadValue(f"«{text}» — иш вақтини тушунмадим. Масалан: 9-18.")


def _day(word: str) -> int | None:
    w = re.sub(r"(дан|гача|га)$", "", _n(word))
    for index, forms in enumerate(_DAY_WORDS):
        if w in forms or any(len(f) > 3 and w.startswith(f[:4]) for f in forms[1::2]):
            return index
    return None


def _days(text: str) -> str:
    words = _n(text)
    if any(p in words for p in ("ҳар куни", "ҳар кун", "каждый день", "ежедневно", "ҳафта бўйи")):
        return "0,1,2,3,4,5,6"
    chosen: set[int] = set()
    last, span = None, False
    for token in re.findall(r"-|–|—|[^\s,;/\-–—]+", text):
        if token in "-–—" or _n(token) in ("дан", "до", "по", "гача"):
            span = True
            continue
        day = _day(token)
        if day is None:
            continue
        if (span or _n(token).endswith("гача")) and last is not None:
            chosen.update(range(last, day + 1) if last <= day else [*range(last, 7), *range(0, day + 1)])
        chosen.add(day)
        span = _n(token).endswith("дан")
        last = day
    if not chosen:
        raise BadValue(f"«{text}» — иш кунларини тушунмадим. Масалан: Ду-Жу ёки Ду, Се, Чо.")
    return ",".join(str(d) for d in sorted(chosen))


def _language(text: str) -> str:
    words = _n(text)
    for lang, stems in LANG_WORDS.items():
        if any(s in words for s in stems):
            return lang
    raise BadValue("Қайси тил: кирилл, лотин ёки рус?")


async def resolve(session: AsyncSession, emp: Employee, key: str, text: str) -> tuple[object, str]:
    """The value as it is stored, and as it is shown. Raises BadValue with what to tell the person."""
    text = " ".join((text or "").split())
    if key == "name":
        if len(text.split()) < 2 or len(text) > 200:
            raise BadValue("Исм учун фамилия ва исм керак (камида икки сўз).")
        return text, text
    if key in ("position", "department"):
        if not 1 <= len(text) <= 100:
            raise BadValue(f"{LABELS[key].capitalize()} 1–100 белги бўлсин.")
        return text, text
    if key in ("manager", "mentor"):
        person = await _person(session, text, emp)
        return (person.id, person.full_name) if person else (None, "—")
    if key == "start_date":
        day = _date(text)
        return day.isoformat(), f"{day:%d.%m.%Y}"
    if key == "hours":
        start, end = _hours(text)
        return [start, end], f"{start}:00–{end}:00"
    if key == "days":
        days = _days(text)
        return days, _days_shown(days)
    if key == "language":
        lang = _language(text)
        return lang, LANG_NAMES[lang]
    raise BadValue("Бу маълумотни ёзиб ўзгартириб бўлмайди.")


# --------------------------------------------------------------------------- the card as it is, and changing it

def _days_shown(days: str) -> str:
    return ", ".join(DAY_SHORT[int(d)] for d in days.split(",") if d.strip().isdigit() and int(d) < 7) or "—"


async def shown(session: AsyncSession, emp: Employee, key: str) -> str:
    """The field as a person reads it."""
    if key in ("manager", "mentor"):
        other_id = emp.manager_id if key == "manager" else emp.mentor_id
        other = await session.get(Employee, other_id) if other_id else None
        return other.full_name if other else "—"
    if key == "start_date":
        return f"{emp.start_date:%d.%m.%Y}" if emp.start_date else "—"
    if key == "hours":
        return f"{emp.work_start_hour}:00–{emp.work_end_hour}:00"
    if key == "days":
        return _days_shown(emp.work_days)
    if key == "language":
        return LANG_NAMES.get(emp.language, emp.language)
    attr = {"name": "full_name"}.get(key, key)
    return str(getattr(emp, attr, "") or "—")


def _set(emp: Employee, key: str, stored: object) -> None:
    if key == "name":
        emp.full_name = str(stored)
    elif key in ("position", "department"):
        setattr(emp, key, str(stored))
    elif key == "manager":
        emp.manager_id = stored or None
    elif key == "mentor":
        emp.mentor_id = stored or None
    elif key == "start_date":
        emp.start_date = date.fromisoformat(str(stored))
    elif key == "hours":
        emp.work_start_hour, emp.work_end_hour = int(stored[0]), int(stored[1])
    elif key == "days":
        emp.work_days = str(stored)
    elif key == "language":
        emp.language = str(stored)


async def record(session: AsyncSession, emp_id: str, key: str, old: str, new: str, actor_id: str | None,
                 how: str = "chat") -> None:
    """One kept change of one field."""
    session.add(ProfileChange(employee_id=emp_id, field=key, old_value=old, new_value=new, changed_by=actor_id,
                              how=how))
    await audit.record(session, "employee.edit", "changed", actor_id=actor_id, object_type="employee",
                       object_id=emp_id, detail=key)


async def apply(session: AsyncSession, emp: Employee, actor_id: str | None, changes: list,
                how: str = "chat") -> list[tuple[str, str, str]]:
    """Change the card and keep each change. `changes`: [field, stored value, …]. Returns what changed:
    (label, was, now); a field set to what it already was is not a change."""
    done = []
    for key, stored, *_ in changes:
        if key not in KEYS:
            continue
        old = await shown(session, emp, key)
        _set(emp, key, stored)
        await session.flush()
        new = await shown(session, emp, key)
        if old != new:
            await record(session, emp.id, key, old, new, actor_id, how)
            done.append((LABELS[key], old, new))
    return done


async def history(session: AsyncSession, emp_id: str, limit: int = 20) -> list[ProfileChange]:
    return list((await session.execute(select(ProfileChange).where(ProfileChange.employee_id == emp_id)
                                       .order_by(ProfileChange.at.desc()).limit(limit))).scalars())
