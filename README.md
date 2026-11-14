# HARI — EMJIEM's AI HR assistant (Telegram)

Built from `HARI_AI_HR_Texnik_Topshiriq_UZ_ 04.11.2026.docx` (the spec) and
`HARI_150_Savol_Javob_IT_Qollanma_UZ.docx` (the 150 scenarios). Section numbers (§) below refer to the spec.
`HARI_HR_AI_Texnik_Topshiriq_UZ.docx` (version 2.0, 13.11.2026 — "spec 2.0" below) extends it; what was built
from it is in its own section.
HARI is a separate bot from @mgmgopsmanagerbot (МИЯ). Bot: **@mgmHaribot**.
It shares no code, database or deployment with МИЯ (repository emjiemai/mgmg-agents).

Everything the bot says is Uzbek Cyrillic, with the polite «сиз» form. People who choose Latin see Latin.
Code, comments and docs are in English.

## What works now (phase 1, spec §26 weeks 5–8)

| Area | What HARI does | Spec |
| --- | --- | --- |
| Employee card | HR fills it in the bot (➕ Янги ходим, 8 steps). The same card sent again makes no second person (A01) | §5 |
| Identity | A one-time link (72 h) binds the person's own Telegram account. One account = one person. Chat text never gives a role | §3, §5, §23 |
| Access | Server-side checks for every document, step and request. A candidate or an unbound account sees nothing internal | §4 |
| Knowledge answers | The AI writes each answer itself. It gets the documents found for the question plus all 150 approved scenarios (`knowledge/samples/HARI-QA-*`, made by `scripts/import_scenarios.py`) as worked examples, so a question worded differently still gets the scenario's rules and next steps. Only what this person may read. It must name what it relied on; the server adds «Манба: …». No basis → "тасдиқланган жавоб топилмади" + a button to send it to HR. Greetings and "are you working?" get a short reply without a source | §10, §12, §13, §24, §30 |
| Safety rules | Pay, bank, other people's data, company finance, passwords, "I'm the director", prompt injection, complaints, legal disputes, personal crisis → fixed approved replies, never sent to the model | §23, HARI-008/042/086–097/126–129 |
| Output checks | Answer with an unknown source, the «сен» form or something that looks like a card/passport/token is stopped | §30 |
| No AI | No key, provider down or over limit: the approved excerpt with its source is shown instead (A12) | §28 |
| Onboarding | 90-day plan (day 1, 2–7, 8–15, 30, 60, 90). Read steps: the employee's «танишдим». Tasks/meetings: proof → the mentor/manager/HR accepts or returns with a reason. «Бажардим» is never acceptance | §9 |
| HR requests | 11 types, number HR-YYYY-MM-NNNN, routed (leave → manager, pay → accountant, legal → lawyer, …), 4 work-hour first-answer deadline. A complaint never goes to the person it's about; about HR → owner (A13). "Answered" and "closed" are separate | §10, §18, §32 |
| Timers | Morning onboarding reminder only on work days and hours, at most 2 planned messages a day (A07). Late first answers → handler + owner, once. Messages are saved before sending, so a crash neither loses nor doubles one (A09) | §10 |
| Audit | Who did what to which object and the outcome, in `audit_events`. The answer log keeps intent, sources and outcome, never the question text | §20, §30 |
| Emergency stop | `HARI_PAUSED=true`: no AI answers, no proactive messages | §1 |

## Spec 2.0 (13.11.2026): what was added

| Area | What HARI does now | Spec 2.0 |
| --- | --- | --- |
| Base instructions | The English agent prompt from §24, versioned (`hari/prompt.py`), goes in front of every AI request, with the server's runtime context (a staff code, role, department, schedule, document versions — never a name or phone) | §24 |
| Settings | `hari_mode` (`shadow` test stage / `live`), and the values a person must fill: owner, independent complaint recipient, daily AI budget, retention policy. In `shadow` a missing one is reported in the system state; in `live` it blocks the workflow that needs it | §25 |
| Same rules in 4 scripts | Pay, passwords, "I'm the director", injection, complaints, other people's data: the same fixed reply for Uzbek Cyrillic, Uzbek Latin, Russian and English wording; Russian speakers get the Russian text | T27 |
| Judging people | A request to read IQ, honesty or character from a face, voice, accent or photo is refused. No IQ/EQ number from a chat; MBTI/DISC is never a reason to reject | §12, T07, T08 |
| Wish to leave | "Кетишни ўйлаяпман" is a reported intent, not a resignation: HR gets one request a day in HARI's own minimal words (never the message), the person is told who sees it, the owner gets one line in the digest. A formal resignation is a request type that also tells the owner at once | §18, T11 |
| Complaints | The person is first asked who the complaint is about (manager / HR / owner / other); it is never routed to them and they cannot see it. About HR or the owner → the independent recipient if one is set, else the other of the two, with an honest note that no independent person is named yet | §2, T16 |
| No reply | Only for verified staff, on delivered work that needs an answer, in their own work minutes (lunch, short Saturday, holidays, leave excluded): 120 min → one private reminder, 240 → their manager, 480 → one line in the owner's digest. Unconfirmed delivery or a failed source → no conclusion at all | §19, T12–T15 |
| Owner digest | Daily at 17:30 on a work day: at most 5 matters needing a decision (fact, effect, owner, deadline), each listed once, never about the owner themself, plus the system state. Friday 16:00: weekly counts for HR and the owner (N/A when nothing to divide by, «номаълум» when there is no data, small groups not broken out) | §16, §20, §28, T28 |
| Sending | A timed-out message is "unknown" and is never resent blindly; a refused connection is retried up to `MAX_RETRIES`. Every automatic message has an idempotency key | §23, T18, T21, T22 |
| Approvals | A role change (`/rol E-0003 hr`, owner only) is a draft until its one-time approval is used: bound to the person, the exact change and the record's version, valid 15 minutes | §25, T19, T20 |
| AI spending | Daily request limit (`LLM_DAILY_CALL_LIMIT`) and a circuit breaker; when either stops AI, HARI answers with the approved excerpt and IT is told once | §23, T23 |
| Data leaving | Before any AI request: phones, e-mails, document numbers, Telegram names and the person's name are masked (`EXTERNAL_PERSONAL_DATA_EGRESS=disabled`) | §10, T30 |
| Knowledge | Passport gains `source_type`, `publisher`, `canonical_url`, `jurisdiction`, `rights`. Rights to index not established → passport and link only, text not indexed. Two sources that disagree → reported as a conflict and sent to HR, no guess. Review date passed → the answer says so. In `live`, a document nobody real approved is refused | §5, §7, §8, T04, T05, T25, T26 |
| Source registry | `knowledge/sources/registry.yaml`: the 28 sources and 8 books of §29–30 with their links, all "reference only" until a person records the rights. `python -m scripts.check_sources` | §29, §30 |
| Candidates | HARI prepares an evidence card (4 criteria, 0–4, weights 40/30/15/15; no evidence = unknown, never 0; no total until every criterion has evidence) and says only "ready for a person" or "more evidence needed". Age, religion, nationality, family are removed before the AI sees the answers. The logic exercise is shown apart, marked unvalidated, and is not part of the card. The interview is scored by a person. A rejection is saved only with the basis that person writes | §11–14, T03, T06, T09, T10 |
| Applicant data | The applicant is told an AI prepares the card and a person decides. They can ask for deletion; HR decides; a legal hold blocks deletion by button and by timer. The timer deletes only in `live` with an approved policy version and number of days | §10, T24 |
| Integrations | `hari/integrations.py`: a source that fails makes its data "unknown" (never "absent") and opens one IT signal. No outside system is connected yet | §9, T15 |
| Database | A column a model gained later is added to the existing table at start (must allow NULL) | — |

Acceptance tests T01–T30 are in `tests/test_acceptance_v2.py` (one test per ID; T29, restoring from a backup,
is an infrastructure drill and is skipped there). End-to-end bot flows for the above: `tests/test_spec2_bot.py`.

**Settings to decide before `HARI_MODE=live`** (all in `.env.example`): `HARI_OWNERS`,
`INDEPENDENT_COMPLAINT_RECIPIENT`, `LLM_DAILY_CALL_LIMIT`, `RETENTION_POLICY_VERSION` + `RETENTION_REJECTED_DAYS`,
`SATURDAY_END_HOUR`, `HOLIDAYS`. HARI does not pick these numbers or people.

**Not built from spec 2.0** (needs an outside system, a licence or a person's decision first): SSO / Microsoft 365
identity and MFA (§8, §23 — identity is still the one-time Telegram link); Verifix, МИЯ, SAP B1 / 1C connectors
(§9); the internet collector with quarantine, malware scan and SSRF blocking (§7 — nothing is fetched, sources are
links); OCR; semantic (embedding) search (§8 — search is keyword BM25); structured personal memory, SWOT and
individual learning plans (§15); OKR/KPI scoring (§16 — only the N/A / unknown rule for ratios); the full
recruiting lifecycle with offers, references and talent pool (§11 — the states are still draft → submitted →
interview → rejected / hired / withdrawn); two independent human raters (§14); the case-learning library, model
card, shadow comparison of model versions and drift monitoring (§21–22); DLP beyond the masking above, admin MFA,
backup and restore drill (§23, T29); the 120-request multilingual acceptance set reviewed by a lawyer and HR (§26);
the owner panel as a web page (§28 — it is the Telegram digest). English fixed texts (rules fire in English, the
reply is Uzbek or Russian).

## Run it

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Linux/macOS: .venv/bin/python
cp .env.example .env                                        # then fill TELEGRAM_BOT_TOKEN (and OPENROUTER_API_KEY)
.venv/Scripts/python -m hari.main
```

The database creates itself (SQLite file `hari.db` locally; PostgreSQL on Render — see below).
Knowledge files load at every start; a changed file replaces its old version, a removed one is retired.

**First HR / owner account** (nobody exists yet to invite them). The person writes `/start` to the bot, which shows
their Telegram ID. Then:

```bash
.venv/Scripts/python -m scripts.add_staff --tg 123456789 --name "Фамилия Исм Отасининг исми" --role hr --department HR --position "HR масъули"
```

Everyone else: HR presses **➕ Янги ходим**, hands the link to the person privately, and they open it themselves.

## Hosting on Render

`render.yaml` is a Render Blueprint: a PostgreSQL 16 database (`hari-db`, basic-256mb) and an always-on web service
(`hari`, starter), both in Frankfurt (closest to Tashkent). About $13 a month together. Free plans don't fit: a free
web service sleeps after 15 minutes, and asleep it sends no reminders; a free database is deleted after 30 days.

1. Render → **New → Blueprint** → connect GitHub → pick **emjiemai/hari** → Apply.
2. It asks for the two secrets: `TELEGRAM_BOT_TOKEN` and `OPENROUTER_API_KEY` (the key can stay empty for now).
3. The first deploy builds, starts, and registers the webhook with Telegram by itself.
   Check: open `https://<service>.onrender.com/health` → `{"status": "ok"}`.
4. First HR account: Render → `hari` → **Shell**, then
   `python -m scripts.add_staff --tg <ID> --name "…" --role hr`.

How it runs there (`BOT_MODE=webhook`): HARI listens on Render's `$PORT`. Telegram posts each update to
`/telegram/webhook`; a request without the right secret header is refused (401). Render checks `/health`, which
also checks the database. The reminder timers run inside the same service. Every push to `main` deploys
(`autoDeploy`); the schema creates missing tables on start. Logs: Render → `hari` → Logs. Emergency stop: set
`HARI_PAUSED=true` under Environment.

**One token, one place.** While Render serves the bot, a local `python -m hari.main` refuses to start (it would take
the bot away from Render). Use a second test bot from @BotFather in your local `.env`. To move the main token back to
your PC: suspend the Render service, then `python -m scripts.unset_webhook`.

## Tests

```bash
.venv/Scripts/python -m pytest -q          # 175 tests + 1 skipped (T29 restore drill), no network
.venv/Scripts/python -m pyflakes hari tests scripts
```

They cover A01, A02, A03, A04, A06, A07, A09, A12 and A13, spec 2.0's T01–T30, plus an end-to-end run of the real dispatcher with a fake
Telegram connection (HR adds someone → link → plan → proof → mentor accepts → question → HR request → answer), and the
Render web server (health check, webhook secret). The same tests run on PostgreSQL with
`HARI_TEST_DATABASE_URL=postgresql://…` set.

## Knowledge documents

One markdown file per document, with a passport (see `hari/knowledge/loader.py`):
`doc_id, title, version, status (draft|approved|retired), classification (public|internal|role), roles, departments,
effective_from/to, owner, approved_by, review_at`. Salary, bank, medical and complaint material is refused — it never
goes into the general HR search. For material from outside the company add `source_type`, `publisher`,
`canonical_url`, `jurisdiction` and `rights: {download, index, train}`; without `rights.index: allowed` only the
passport and link are kept (see the loader's docstring).

`knowledge/samples/` holds **sample** texts written from the spec (every title starts «НАМУНА»). HARI uses them
until the real, approved documents go into `knowledge/docs/` and `KNOWLEDGE_DIR=knowledge/docs` is set.
Real staff data only after the checks in §28 pass (§31).

## Layout

```
hari/
  main.py            start-up: schema, knowledge, polling, scheduler
  config.py          settings (.env)
  db/                models (§22) and sessions
  access.py          who may see / do what (§4)
  employees.py       cards, invites, identity binding (§5)
  knowledge/         loader (passport, chunks) and permission-filtered search (§12)
  guard.py           fixed safety rules and output checks (§23, §30)
  answer.py          question → answer pipeline (§10, §20)
  llm.py             OpenRouter client with model fallback
  onboarding.py      90-day plan and human acceptance (§9)
  cases.py           HR requests register and routing (§32)
  scheduler.py       timers and reliable sending (§10; spec 2.0 §20)
  outbox.py          idempotent message queue and the daily limit
  prompt.py          versioned base instructions and runtime context (spec 2.0 §24)
  nonresponse.py     the no-reply rule (spec 2.0 §19)
  digest.py          owner's daily digest, weekly report, system state (spec 2.0 §20, §28)
  approvals.py       one-time approvals bound to an action (spec 2.0 §25)
  budget.py          AI request limit and circuit breaker (spec 2.0 §23)
  egress.py          masking before data leaves for the AI provider (spec 2.0 §10)
  integrations.py    health of outside sources (spec 2.0 §9)
  retention.py       deletion requests, legal hold, retention timer (spec 2.0 §10)
  apply/rubric.py    the candidate evidence card (spec 2.0 §14)
  bot/               Telegram handlers, texts, keyboards
scripts/add_staff.py first HR / owner account
scripts/unset_webhook.py  move the bot token back to polling (local PC)
render.yaml          Render Blueprint (database + web service)
knowledge/           samples/ (for now), docs/ (real documents), sources/registry.yaml (spec 2.0 §29)
scripts/check_sources.py  check and print the source registry
tests/
```

## Not done yet (next phases, spec §26)

- **Weeks 9–12:** link to МИЯ (task / proof / KPI read from МИЯ, never computed here — §15, §21; needs an API or
  read-only access from Ulugbek), training and skill evidence (§14), exit checklist and access revocation (§25),
  dashboard and daily 18:15 / weekly reports for Зайнаб and the owner (§17), 7-day access review (§9).
- **Weeks 13–16:** vacancies, candidates, calendar, interview packet (§6–7).
- **Before the pilot:** real approved SOPs; the 150 scenarios as an automated test set (§13, §28); PostgreSQL on a
  server in Uzbekistan and masking personal data before the AI (§19 — needs the lawyer's check of the AI provider);
  open forms kept in the database instead of memory (a restart now drops a half-filled form); Russian texts for
  menus and forms (the safety replies have them); revoke and reissue the bot token (it was shared in a chat).
