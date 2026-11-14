"""What may leave the server for the AI provider (spec 2.0 §10, §25 `external_personal_data_egress`; T30).

While egress is "disabled" (the default until the lawyer accepts the provider), every AI request
passes through `scrub`: phones, e-mails, card, account, passport and PINFL numbers, Telegram names
and any names the caller knows belong to a person are replaced by a marker. The model still gets
the work-related text; it does not get who the person is.

`redact_protected` is a different rule (§14, T06): age, religion, nationality, family and similar
traits are taken out of what a candidate wrote before it is judged, so the judgement cannot move
when only such a trait changes.
"""

from __future__ import annotations

import re

from hari.config import settings

MASK = "[шахсий маълумот яширилди]"
TRAIT = "[ишга алоқасиз маълумот олиб ташланди]"

_IDENTIFIERS = [
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # e-mail
    re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{4,31}\b"),  # Telegram name
    re.compile(r"\b(?:\d[ -]?){16}\b"),  # card number
    re.compile(r"\b\d{20}\b"),  # bank account
    re.compile(r"\b\d{14}\b"),  # PINFL
    re.compile(r"\b[A-Z]{2} ?\d{7}\b"),  # passport
    re.compile(r"(?<!\d)\+?998[ \-()]*\d{2}[ \-()]*\d{3}[ \-]*\d{2}[ \-]*\d{2}(?!\d)"),  # +998 90 123 45 67
    re.compile(r"(?<!\d)\(?\d{2}\)?[ \-]\d{3}[ \-]\d{2}[ \-]\d{2}(?!\d)"),  # 90 123 45 67
    re.compile(r"(?<!\d)\d{9}(?!\d)"),  # 901234567
    re.compile(r"(?<!\d)\+\d{10,15}(?!\d)"),  # any other international number
]

# Protected traits and their stand-ins (spec 2.0 §14). Matched in Cyrillic, Latin Uzbek and Russian.
_PROTECTED = [
    # age: "25 ёшдаман", "ёшим 25", "мне 25 лет", "1998 йилда туғилганман"
    r"\b\d{1,2}\s*(?:ёш\w*|yosh\w*|лет|год(?:а|ов)?)\b", r"\b(?:ёшим|yoshim|возраст)\s*[:\-]?\s*\d{1,2}\b",
    r"\b(?:19|20)\d{2}\s*[- ]?(?:йил\w*|yil\w*|год[ау]?)\s*(?:туғилган\w*|tug.?ilgan\w*|рождени\w*)",
    r"\b(?:туғилган\w*|tug.?ilgan\w*|родил(?:ся|ась))\b[^.,;\n]{0,25}",
    # religion
    r"\b(?:мусулмон\w*|musulmon\w*|мусульман\w*|христиан\w*|xristian\w*|православ\w*|яҳудий\w*|yahudiy\w*|"
    r"иудей\w*|буддист\w*|атеист\w*|намоз\w*|namoz\w*|рўза\w*|ro.?za\w*|ҳижоб\w*|hijob\w*|хиджаб\w*|"
    r"диндор\w*|dindor\w*|верующ\w*|масжид\w*|masjid\w*|мечет\w*|черков\w*|церк\w*)",
    # nationality / ethnicity
    r"\b(?:миллатим\w*|millatim\w*|миллати\w*|millati\w*|национальност\w*|по национальности)\b[^.,;\n]{0,25}",
    r"\b(?:ўзбекман|o.?zbekman|русман|rusman|тожикман|tojikman|қозоқман|qozoqman|корейсман|koreysman|"
    r"татарман|tatarman|я (?:узбек|узбечка|русский|русская|таджик|таджичка|казах|казашка|кореец|кореянка|"
    r"татарин|татарка))\b",
    # family, pregnancy
    r"\b(?:турмушга чиққан\w*|turmushga chiqqan\w*|уйланган\w*|uylangan\w*|ажрашган\w*|ajrashgan\w*|"
    r"бўйдоқ\w*|bo.?ydoq\w*|замужем|женат\w*|не женат|не замужем|разведен\w*|разведён\w*|холост\w*|"
    r"ҳомиладор\w*|homilador\w*|беременн\w*|декрет\w*|dekret\w*)",
    r"\b\d{1,2}\s*(?:та|нафар|ta|nafar)?\s*(?:фарзанд\w*|farzand\w*|бола\w*|bola\w*|дет(?:ей|и)|ребён\w*|ребен\w*)",
    r"\b(?:фарзандим\w*|farzandim\w*|болаларим\w*|bolalarim\w*|у меня (?:дети|ребёнок|ребенок))\b",
    # disability, politics
    r"\b(?:ногирон\w*|nogiron\w*|инвалид\w*)", r"\b(?:партия\w* аъзоси|partiya\w* a.?zosi|член партии)\b",
]
_PROTECTED_RE = re.compile("|".join(_PROTECTED), re.IGNORECASE)


def scrub(text: str, known: tuple[str, ...] | list[str] = ()) -> str:
    """Mask identifiers. `known` are strings the caller knows identify a person (a full name, a phone)."""
    for value in sorted({k.strip() for k in known if k and len(k.strip()) >= 3}, key=len, reverse=True):
        text = re.sub(re.escape(value), MASK, text, flags=re.IGNORECASE)
        for word in value.split():  # the name's parts on their own: "Вали" from "Алиев Вали"
            if len(word) >= 4 and not word.isdigit():
                text = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", MASK, text, flags=re.IGNORECASE)
    for pattern in _IDENTIFIERS:
        text = pattern.sub(MASK, text)
    return text


def outbound(text: str, known: tuple[str, ...] | list[str] = ()) -> str:
    """The text as it may leave for the AI provider under the current setting."""
    if settings.external_personal_data_egress.strip().lower() == "allowed":
        return text
    return scrub(text, known)


def redact_protected(text: str) -> str:
    return _PROTECTED_RE.sub(TRAIT, text or "")
