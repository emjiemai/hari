"""The evidence card for a candidate (spec 2.0 §13, §14; T06, T09, T10).

Four criteria, each scored 0–4 from evidence, with sample weights (40/30/15/15). The weights and
criteria are an engineering starting point: they are fixed before candidates are looked at, the
same for everyone applying to a role, and are confirmed by HR after a pilot — they are not a cutoff.

- A criterion without evidence is "unknown", never 0. While any criterion is unknown there is no
  total at all (T09).
- The total is 25 × the weighted average (3, 4, 2, 3 → 78.75). It is a summary for a person to
  read; no total hires or rejects anyone.
- The structured interview is scored by the person who held it, never by HARI.
- The short logic exercise applicants answer is not a validated cognitive test: it is shown to the
  reader on its own line and is not part of this card (T10).
"""

from __future__ import annotations

from dataclasses import dataclass

RUBRIC_VERSION = "1"


@dataclass(frozen=True)
class Criterion:
    key: str
    label: str  # Uzbek, shown to HR
    weight: int  # per cent
    evidence: str  # what counts as evidence (English: goes into the model prompt)
    by_human: bool = False


CRITERIA: tuple[Criterion, ...] = (
    Criterion("work_sample", "Касбий иш намунаси", 40,
              "the answers to the job questions: is the result correct, practical and checkable"),
    Criterion("interview", "Тузилмали суҳбат", 30,
              "concrete examples given in a structured interview with the same core questions for the role",
              by_human=True),
    Criterion("learning", "Ўрганиш ва муаммо ечиш", 15,
              "how the person reasons about a problem and adjusts a solution when given new information"),
    Criterion("collaboration", "Ишга алоқадор ҳамкорлик", 15,
              "job-related communication and documenting: clear, respectful, complete written answers"),
)
BY_KEY = {c.key: c for c in CRITERIA}

SCALE = {0: "нотўғри ёки хавфли", 1: "катта камчилик", 2: "қисман тўғри", 3: "мустақил ва текшириладиган натижа",
         4: "тўғри натижа ва истиснони асосли бошқариш"}

READY = "ready"
MORE = "more"
RECOMMENDATIONS = {READY: "Инсон кўриб чиқишига тайёр", MORE: "Қўшимча далил керак"}


def clean_score(value) -> int | None:  # noqa: ANN001 — whatever the model or a button sent
    """An integer 0–4, or None. A missing or odd value is unknown — never zero."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 4 else None


def weighted_total(scores: dict[str, int | None]) -> float | None:
    """25 × weighted average over all criteria; None while any of them has no evidence."""
    if any(scores.get(c.key) is None for c in CRITERIA):
        return None
    return round(25 * sum(scores[c.key] * c.weight for c in CRITERIA) / 100, 2)


def build_card(criteria: dict[str, dict], recommendation: str, summary: str, missing: list[str]) -> dict:
    """The stored card. `criteria` maps a key to {"score", "evidence", optional "by"}."""
    rows = {}
    for c in CRITERIA:
        row = criteria.get(c.key) or {}
        score = clean_score(row.get("score"))
        evidence = str(row.get("evidence") or "").strip()[:400]
        if score is not None and not evidence:
            score = None  # a number without evidence is not a score
        rows[c.key] = {"score": score, "evidence": evidence, **({"by": row["by"]} if row.get("by") else {})}
    return {
        "rubric_version": RUBRIC_VERSION,
        "criteria": rows,
        "total": weighted_total({k: v["score"] for k, v in rows.items()}),
        "recommendation": recommendation if recommendation in RECOMMENDATIONS else MORE,
        "summary": summary.strip()[:600],
        "missing": [str(m).strip()[:200] for m in missing if str(m).strip()][:5],
    }


def with_human_score(card: dict | None, key: str, score: int, by: str) -> dict:
    """A person's score for one criterion (the interview), recorded with who gave it."""
    card = card or build_card({}, MORE, "", [])
    criteria = {k: dict(v) for k, v in card["criteria"].items()}
    criteria[key] = {"score": clean_score(score), "evidence": "инсон баҳоси (суҳбат)", "by": by}
    return {**card, "criteria": criteria,
            "total": weighted_total({k: v["score"] for k, v in criteria.items()})}


def card_lines(card: dict) -> list[str]:
    lines = ["Мезонлар (0–4; далил йўқ бўлса «номаълум», 0 эмас):"]
    for c in CRITERIA:
        row = card.get("criteria", {}).get(c.key) or {}
        score = row.get("score")
        if score is None:
            why = "суҳбатдан кейин инсон баҳолайди" if c.by_human else (row.get("evidence") or "далил етарли эмас")
            lines.append(f"• {c.label} ({c.weight}%): номаълум — {why}")
        else:
            lines.append(f"• {c.label} ({c.weight}%): {score} — {row.get('evidence') or SCALE[score]}")
    total = card.get("total")
    lines.append("Умумий балл: номаълум (барча мезонда далил бўлгач ҳисобланади)" if total is None
                 else f"Умумий балл: {total:g} / 100 — бу қабул ёки рад чегараси эмас")
    if card.get("missing"):
        lines.append("Етишмаётган далил: " + "; ".join(card["missing"]))
    return lines
