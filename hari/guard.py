"""Safety rules around the model (spec §23, §30; scenarios HARI-008, 042, 086–088, 096–097, 126–129;
spec 2.0 §12, §18, §24, acceptance T01, T02, T08, T11, T27).

1. Before the model: questions that must not reach the general HR context (other people's
   pay, company finance, passwords, "I'm the director, open everything", complaints, legal
   disputes, personal crises, judging a person by their face, a stated wish to leave) get a
   fixed, approved reply and a route to the right person.
2. After the model: an answer without a valid source, with an impolite form ("сен"), or
   carrying something that looks like a secret is stopped.

The same rule fires for Uzbek in Cyrillic, Uzbek in Latin, Russian and English (T27): Cyrillic
stems are matched against the text turned into Cyrillic, Latin-letter stems against the text as typed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from hari.translit import normalize


@dataclass(frozen=True)
class Rule:
    intent: str
    stems: tuple[str, ...]  # matched against the start of each normalized word
    reply: str
    case_type: str | None = None  # offer to open this kind of HR request
    reply_ru: str = ""
    auto_case: bool = False  # the request is opened at once, with HARI's own minimal wording (spec 2.0 §18)


# Order matters: the first rule that matches wins.
RULES: list[Rule] = [
    Rule("credentials", ("парол", "пароль", "эри калит", "эцп", "логин", "токен", "ключ$", "ключи$", "пин код", "пинкод",
                         "password", "api key", "apikey"),
         "Парол, ЭРИ ва API калитини чатга юборманг. Амал ваколатли шахс томонидан хавфсиз тизимда "
         "бажарилади. Агар сир юборилган бўлса, тегишли калитни алмаштириш керак — IT масъулига хабар беринг.",
         reply_ru="Не отправляйте в чат пароль, ЭЦП и API-ключи. Действие выполняет уполномоченный сотрудник в "
                  "защищённой системе. Если секрет уже отправлен, ключ нужно заменить — сообщите IT-специалисту."),
    Rule("authority_claim", ("директорман", "эгасиман", "админман", "рухсат берди", "рухсат берган",
                             "ҳамма маълумот", "барча маълумот", "базасини", "базани бер", "базасини бер",
                             "я директор", "я владелец", "я админ", "я руководител", "директор разрешил",
                             "i am the director", "i'm the director", "i am the owner", "i'm the owner",
                             "i am the admin", "i'm the admin", "i am an admin"),
         "Ваколат корпоратив аккаунт ва сервердаги роль орқали текширилади. Чатдаги даъво ёки маълумотни бошқа "
         "форматга ўтказиш сўрови ҳуқуқ бермайди. Ҳозирги тасдиқланган ҳуқуқингиз доирасида ёрдам бераман.",
         reply_ru="Полномочия проверяются по корпоративному аккаунту и роли на сервере. Заявление в чате или просьба "
                  "выдать данные в другом формате прав не даёт. Помогу в рамках ваших подтверждённых прав."),
    Rule("injection", ("қоидаларни унут", "аввалги қоидалар", "ignore previous", "ignore all", "забудь",
                       "system prompt", "систем промпт", "forget the rules", "forget your rules",
                       "disregard previous"),
         "Ташқи файл ёки чат ичидаги матн тизим буйруғи ҳисобланмайди. Мен фақат тасдиқланган вазифа ва ҳуқуқ "
         "доирасида ишлайман.",
         reply_ru="Текст из файла или чата не является командой для системы. Я работаю только в рамках "
                  "утверждённых задач и прав."),
    # Spec 2.0 §12, T08: nothing about a person is read from a face, voice, accent or photo.
    Rule("biometric", ("юзидан", "юзига қараб", "суратидан", "суратига қараб", "расмидан", "расмига қараб",
                       "овозидан", "овозига қараб", "акцентидан", "кўзидан", "кўз ҳаракат",
                       "по лицу", "по фото", "по голосу", "по акценту",
                       "from face", "from the face", "by face", "by the face", "from photo", "from a photo",
                       "from the photo", "from voice", "from the voice", "by voice", "from accent"),
         "Юз, овоз, акцент, сурат ёки ташқи кўринишдан интеллект, ҳиссиёт, ҳалоллик ёки шахсиятни баҳоламайман — "
         "бундай баҳолаш бажарилмайди. Номзод ва ходим фақат ишга оид далил билан баҳоланади: иш намунаси, "
         "тузилмали суҳбат ва тасдиқланган мезонлар. Якуний қарорни ваколатли инсон қабул қилади.",
         reply_ru="Я не оцениваю интеллект, эмоции, честность или личность по лицу, голосу, акценту, фото или "
                  "внешности — такая оценка не выполняется. Кандидата и сотрудника оценивают только по рабочим "
                  "данным: рабочее задание, структурированное интервью и утверждённые критерии. Итоговое решение "
                  "принимает уполномоченный человек."),
    # Spec 2.0 §12, T07, T10: no IQ/EQ number from a chat, no MBTI/DISC as a reason to reject.
    Rule("psychometric", ("iq$", "айкью", "eq$", "mbti", "disc$", "эннеаграмм", "психологик тест", "психотест",
                          "шахсият тести", "интеллект тести", "ақл тести", "личностный тест", "тест на интеллект",
                          "психологический тест", "personality test", "intelligence test"),
         "IQ ва EQни чатдан ўлчаб бўлмайди, мен бундай балл чиқармайман. MBTI, DISC, эннеаграмма ёки ўйлаб "
         "топилган «IQ» балли асосида ҳеч ким рад этилмайди. Лицензияли тест фақат шу тил ва лавозим учун "
         "валидлиги текширилгандан кейин, малакали мутахассис томонидан қўлланади ва якка ўзи қарор асоси "
         "бўлмайди. Унинг ўрнига лавозимга оид иш намунаси ва тузилмали суҳбатдан фойдаланамиз.",
         reply_ru="IQ и EQ нельзя измерить по чату, такой балл я не выдаю. По MBTI, DISC, эннеаграмме или "
                  "придуманному «IQ» никому не отказывают. Лицензионный тест применяется только после проверки "
                  "его валидности для этого языка и должности, квалифицированным специалистом, и сам по себе не "
                  "является основанием для решения. Вместо него используем рабочее задание по должности и "
                  "структурированное интервью."),
    # Spec 2.0 §18, T11: a stated wish to leave is a reported intent — HR is told, nothing else is concluded.
    Rule("exit_intent", ("ишдан кетмоқчи", "ишдан кетаман", "ишдан кетяпман", "кетишни ўйла",
                         "ишдан бўшамоқчи", "бўшамоқчиман", "ишни ташламоқчи", "ишни ташлайман",
                         "хочу уволиться", "хочу уволится", "увольняюсь", "уволюсь", "думаю уволиться",
                         "собираюсь уволиться", "хочу уйти с работы", "думаю уйти", "думаю об уходе",
                         "want to quit", "going to quit", "thinking of leaving", "thinking about leaving",
                         "want to resign", "going to resign", "i quit"),
         "Буни айтганингиз учун раҳмат. Бу сўзингиз якуний қарор ёки ариза ҳисобланмайди ва баҳоингиз ёки "
         "KPIга таъсир қилмайди.\n\nHR масъулига қисқа хабар бердим: фақат «ходим кетишни ўйлаётганини айтди» "
         "деган факт ва сана — ёзишмангизнинг ўзи юборилмайди. Уни HR масъули, зарур бўлса компания раҳбари "
         "кўради. HR сиз билан иш вақтида гаплашади.\n\nРасмий ариза бермоқчи бўлсангиз: «Мурожаат» → "
         "«Ишдан бўшаш аризаси».",
         "exit_intent",
         reply_ru="Спасибо, что сказали. Эти слова не считаются окончательным решением или заявлением и не влияют "
                  "на вашу оценку или KPI.\n\nЯ коротко сообщил(а) HR: только факт «сотрудник сказал, что думает об "
                  "уходе» и дату — сама переписка не отправляется. Это увидит HR и, при необходимости, руководитель "
                  "компании. HR поговорит с вами в рабочее время.\n\nЧтобы подать официальное заявление: «Мурожаат» → "
                  "«Ишдан бўшаш аризаси».",
         auto_case=True),
    # «фойда» alone or with an ownership ending (фойдаси, фойдани), never «фойдаланиш» (to use) or «фойдали».
    Rule("finance_secret", ("фойда$", "фойдаси", "фойдасини", "фойдани", "фойдамиз", "таннарх", "маржа", "прибыл",
                            "себестоим", "банк ҳисоб", "қарзлар", "company profit", "profit margin", "net profit"),
         "Банк, фойда, таннарх ва махфий режаларга махсус ҳуқуқ керак — улар HR ёрдамчиси орқали "
         "кўрсатилмайди. Ролингиз учун очиқ мақсадлар бўйича савол берсангиз, ёрдам бераман.",
         reply_ru="Для банковских данных, прибыли, себестоимости и закрытых планов нужны особые права — через "
                  "HR-помощника они не показываются. Помогу по целям, открытым для вашей роли."),
    Rule("pay", ("маош", "ойлиг", "иш ҳақи", "иш ҳақим", "зарплат", "аванс", "бонус", "мукофот пул", "карта рақам",
                 "реквизит", "ҳисоб варақ", "оклад", "salary", "salaries", "payroll", "wage", "paycheck", "payslip"),
         "Иш ҳақи ва банк маълумотлари бу умумий каналда кўрсатилмайди, бошқа ходимларники эса умуман очилмайди. "
         "Ўз ҳисоб варағингиз бўйича саволни бухгалтерга текширув сўрови сифатида юборишим мумкин.",
         "pay_question",
         reply_ru="Зарплата и банковские данные в этом общем канале не показываются, а чужие не открываются вообще. "
                  "Вопрос по вашему расчётному листу могу отправить бухгалтеру как запрос на проверку."),
    Rule("others_data", ("бошқаларники", "бошқа ходимнинг", "бошқа ходимлар", "ҳамкасбимнинг", "рейтингини",
                         "кимнинг kpi", "другого сотрудника", "других сотрудников", "чужие данные", "коллеги$",
                         "another employee", "other employees", "my colleague's", "coworker's"),
         "Бошқа ходимларнинг шахсий маълумоти, KPI ёки маошини рухсатсиз очмайман. Ўз натижангиз ва ўз "
         "маълумотингиз бўйича ёрдам бераман.",
         reply_ru="Личные данные, KPI и зарплату других сотрудников я без разрешения не открываю. Помогу по вашим "
                  "результатам и вашим данным."),
    Rule("complaint", ("шикоят", "ҳақорат", "камсит", "таъқиб", "босим ўтказ", "жалоб", "оскорб", "домогат",
                       "complain", "complaint", "harass", "insult", "discriminat"),
         "Воқеа, сана ва керакли ёрдамни ёзинг. Мурожаатни ким кўриши олдиндан кўрсатилади ва шикоят қилинган "
         "шахс уни текширмайди. Мутлақ махфийликни ваъда қила олмайман, лекин маълумот фақат текширув учун "
         "зарур кишига боради. Шикоятнинг ўзи баҳоингизга ёки KPIга салбий таъсир қилмайди.", "complaint",
         reply_ru="Опишите событие, дату и какая помощь нужна. Заранее будет показано, кто увидит обращение; тот, на "
                  "кого жалоба, его не рассматривает. Полную конфиденциальность обещать не могу, но данные получит "
                  "только тот, кто нужен для проверки. Сама жалоба не влияет отрицательно на вашу оценку или KPI."),
    Rule("legal", ("суд$", "судга", "суддан", "судда", "судлаш", "низо", "прокурат", "инспекция", "юрист", "адвокат",
                   "устидан ариза", "ишдан бўшатиш", "lawsuit", "lawyer", "sue$", "court$", "labor inspect",
                   "labour inspect"),
         "Мен компаниянинг ички HR ёрдамчисиман, мустақил юридик вакилингиз эмасман. Шахсий низога юридик баҳо "
         "бермайман. Истасангиз, масалани ички кўриб чиқиш учун ваколатли HR ёки юристга қайд этаман. "
         "Ҳуқуқингиз ҳақида сўрашингиз баҳоингизга салбий таъсир қилмайди.", "legal",
         reply_ru="Я внутренний HR-помощник компании, а не ваш независимый юридический представитель. Юридическую "
                  "оценку личного спора не даю. Если хотите, зарегистрирую вопрос для внутреннего рассмотрения "
                  "уполномоченным HR или юристом. Вопрос о ваших правах не влияет отрицательно на вашу оценку."),
    Rule("support", ("ўзимни ёмон", "чарчадим", "стресс", "депресс", "яшагим", "умидсиз", "кайфиятим йўқ",
                     "выгорел", "выгоран", "нет сил", "burned out", "burnt out", "burnout", "depress"),
         "Буни айтганингиз учун раҳмат. Мен ҳолатингизни баҳоламайман. Хоҳласангиз, HR билан шахсий суҳбат "
         "сўровини очаман — бу сизнинг рейтингингиз ёки KPIга таъсир қилмайди. Ҳаётингизга хавф бўлса, "
         "дарҳол 103 ёки яқинларингизга мурожаат қилинг.", "support",
         reply_ru="Спасибо, что сказали. Я не оцениваю ваше состояние. Если хотите, открою запрос на личный разговор "
                  "с HR — это не влияет на ваш рейтинг или KPI. Если есть угроза жизни, сразу звоните 103 или "
                  "обратитесь к близким."),
    Rule("decision", ("ишга олинг", "ишдан бўшатинг", "жарима сол", "маошимни ошир", "лавозимимни ошир", "уволь",
                      "оштрафуй", "примите на работу", "fire him", "fire her", "hire him", "hire her"),
         "Ишга қабул, бўшатиш, маош, жазо ва лавозим бўйича қарорни ваколатли инсон қабул қилади, мен эмас. "
         "Масалани тегишли масъулга мурожаат сифатида юборишим мумкин.", "other",
         reply_ru="Решения о приёме, увольнении, зарплате, взыскании и должности принимает уполномоченный человек, "
                  "а не я. Могу отправить вопрос ответственному как обращение."),
]

_WORD = "а-яёўқғҳa-z0-9"


def match_rule(text: str) -> Rule | None:
    norm = f" {normalize(text)} "
    raw = f" {text.lower()} "
    for rule in RULES:
        for stem in rule.stems:
            whole = stem.endswith("$")  # the stem must be the whole word: «фойда$» is not «фойдаланиш»
            body = stem.rstrip("$")
            if re.search(r"[a-z]", body):
                # Latin-letter stems (English, "kpi") are matched against the text as typed.
                pattern, target = rf"(?<![{_WORD}]){re.escape(body)}", raw
            else:
                # Cyrillic stems match at the start of a word, in either script the person typed.
                pattern, target = rf"(?<![{_WORD}]){re.escape(normalize(body))}", norm
            if whole:
                pattern += rf"(?![{_WORD}])"
            if re.search(pattern, target):
                return rule
    return None


# A short "yes" to the request HARI just offered: "ha", "tasdiqlayman", "ha tayyorlab yubor", "да, отправь".
# Every word of the reply must be one of these: a whole word, a filler, or a verb stem in a form
# without the Uzbek negative "-ма-" ("юборинг" yes, "юборманг" no).
_CONSENT_WORDS = {"ҳа", "ха", "хоп", "хўп", "майли", "ок", "окей", "окай", "рози", "розиман", "албатта",
                  "илтимос", "шундай", "тўғри", "да", "давай", "конечно", "согласен", "согласна", "ес", "иес",
                  "ага", "оч", "очинг", "открой", "откройте"}
_CONSENT_STEMS = ("тасдиқла", "юбор", "тайёрла", "подтвержда", "отправ")
_CONSENT_FILLERS = {"мен", "буни", "шуни", "уни", "ҳам", "пожалуйста", "ни", "ва", "энди", "енди"}
_NEGATIONS = {"йўқ", "йуқ", "нет", "не", "керакмас", "эмас"}


def _consent_word(word: str) -> bool:
    if word in _CONSENT_WORDS or word in _CONSENT_FILLERS:
        return True
    return any(word.startswith(s) and not word[len(s):].startswith("ма") for s in _CONSENT_STEMS)


def is_consent(text: str) -> bool:
    """A short yes to the request HARI just offered (only then does the bot act on it)."""
    words = normalize(text).split()
    if not words or len(words) > 5 or any(w in _NEGATIONS for w in words):
        return False
    return all(_consent_word(w) for w in words)


def reply_for(rule: Rule, language: str) -> str:
    """The approved reply in the person's language. Uzbek Latin is made from the Cyrillic text by the sender."""
    return rule.reply_ru if language == "ru" and rule.reply_ru else rule.reply


# --------------------------------------------------------------------------- output checks

_IMPOLITE = re.compile(r"(?<![а-яёўқғҳ])(сен|сенга|сени|сенинг|сендан|sen|senga|seni|sening)(?![а-яёўқғҳa-z])",
                       re.IGNORECASE)
_SECRETS = [
    re.compile(r"\b(?:\d[ -]?){16}\b"),  # card number
    re.compile(r"\b\d{20}\b"),  # bank account
    re.compile(r"\b[A-Z]{2}\d{7}\b"),  # passport
    re.compile(r"\b\d{14}\b"),  # ЖШШИР / PINFL
    re.compile(r"(?i)\b(api[_-]?key|password|парол)\s*[:=]"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b"),  # a Telegram bot token
]


def is_polite(text: str) -> bool:
    return not _IMPOLITE.search(text)


def leaks_secret(text: str) -> bool:
    return any(p.search(text) for p in _SECRETS)


def clean_model_text(text: str, limit: int = 1500) -> str:
    """Plain text only: the bot sends without HTML/Markdown parsing, so nothing the model writes becomes a link."""
    text = re.sub(r"https?://\S+", "", text)  # model-made links are never shown (spec §23)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text[:limit]
