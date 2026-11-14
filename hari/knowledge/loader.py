"""Load knowledge documents (markdown + front matter passport) into the search index (spec §12).

A document file looks like:

    ---
    doc_id: SOP-HR-001
    title: Таътил олиш тартиби
    version: "2"
    status: approved            # draft | approved | retired
    classification: internal    # public | internal | role
    roles: ""                   # for "role": comma list of roles or positions
    departments: ""             # for "role": comma list of departments
    effective_from: 2026-11-01
    effective_to:
    owner: HR
    approved_by: Зайнаб
    review_at: 2027-05-01
    ---
    ## Section
    text...

Confidential classes (salary, bank, medical, complaints) are refused and never indexed —
they do not belong in the general HR search (spec §4–5). A changed file replaces its old
version in the index; a removed file is retired.

Spec 2.0 §5, §7 add optional passport lines for material that came from outside the company:

    source_type: professional_guidance   # law | company_policy | professional_guidance | recommendation
    publisher: CIPD
    canonical_url: https://…
    jurisdiction: professional_guidance
    rights: {download: allowed, index: review, train: false}

- `rights.index` other than "allowed" (or missing while a publisher / canonical_url is given) means the
  right to index the text is not established: the passport is kept as a reference with its link, and
  the text is NOT indexed or served (RIGHTS_UNKNOWN, T25).
- `rights.train` must be false: HARI trains on nothing.
- In live mode a document nobody real approved (empty or a «НАМУНА» marker in `approved_by`) is refused
  (APPROVAL_REQUIRED). In the test stage such sample documents load, and are counted in the system state.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit
from hari.config import settings
from hari.db.models import KnowledgeChunk, KnowledgeDocument
from hari.digest import is_placeholder
from hari.translit import normalize

ALLOWED_CLASSES = {"public", "internal", "role"}
ALLOWED_STATUSES = {"draft", "approved", "retired"}
SOURCE_TYPES = {"law", "company_policy", "professional_guidance", "recommendation"}
RIGHTS_VALUES = {"allowed", "review", "denied"}
REQUIRED = ("doc_id", "title", "version", "status", "classification")
CHUNK_CHARS = 2400  # about 400–800 tokens of Uzbek text (spec §12 step 5)


@dataclass
class LoadResult:
    loaded: list[str]
    unchanged: list[str]
    refused: list[tuple[str, str]]
    retired: list[str]
    reference: list[str] = field(default_factory=list)  # passport kept, text not indexed (rights not established)


def _split_front_matter(raw: str) -> tuple[dict, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", raw, re.DOTALL)
    if not match:
        raise ValueError("no front matter")
    meta = yaml.safe_load(match.group(1)) or {}
    if not isinstance(meta, dict):
        raise ValueError("front matter is not a mapping")
    return meta, match.group(2)


def index_right(meta: dict) -> str | None:
    """allowed | review | denied for outside material; None for the company's own documents."""
    rights = meta.get("rights")
    outside = bool(str(meta.get("publisher") or "").strip() or str(meta.get("canonical_url") or "").strip())
    if rights is None:
        return "review" if outside else None
    if not isinstance(rights, dict):
        raise ValueError("rights must be a mapping: {download, index, train}")
    if str(rights.get("train", "false")).strip().lower() not in ("false", "no", "0", "none", ""):
        raise ValueError("rights.train must be false: HARI is not trained on documents")
    value = str(rights.get("index") or "review").strip().lower()
    value = {"true": "allowed", "yes": "allowed", "false": "denied", "no": "denied"}.get(value, value)
    if value not in RIGHTS_VALUES:
        raise ValueError(f"rights.index '{value}' unknown")
    return value


def _as_date(value) -> date | None:  # noqa: ANN001 — YAML gives date, str or None
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def chunk_markdown(body: str) -> list[tuple[str, str]]:
    """(section title, text) pieces: split at headings, then by size with one paragraph of overlap."""
    sections: list[tuple[str, list[str]]] = []
    title, paragraphs = "", []
    for block in re.split(r"\n\s*\n", body.strip()):
        heading = re.match(r"^#{1,4}\s+(.+)$", block.strip().splitlines()[0]) if block.strip() else None
        if heading:
            if paragraphs:
                sections.append((title, paragraphs))
            title = heading.group(1).strip()
            rest = "\n".join(block.strip().splitlines()[1:]).strip()
            paragraphs = [rest] if rest else []
        elif block.strip():
            paragraphs.append(block.strip())
    if paragraphs:
        sections.append((title, paragraphs))

    chunks: list[tuple[str, str]] = []
    for sec_title, paras in sections:
        current: list[str] = []
        for para in paras:
            if current and len("\n\n".join(current + [para])) > CHUNK_CHARS:
                chunks.append((sec_title, "\n\n".join(current)))
                current = [current[-1]]  # overlap
            current.append(para)
        if current:
            chunks.append((sec_title, "\n\n".join(current)))
    return chunks


async def load_directory(session: AsyncSession, directory: str | Path) -> LoadResult:
    result = LoadResult([], [], [], [])
    seen: set[str] = set()
    for path in sorted(Path(directory).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        try:
            meta, body = _split_front_matter(raw)
            missing = [k for k in REQUIRED if not str(meta.get(k) or "").strip()]
            if missing:
                raise ValueError(f"missing {', '.join(missing)}")
            cls, status = str(meta["classification"]).lower(), str(meta["status"]).lower()
            if cls not in ALLOWED_CLASSES:
                raise ValueError(f"classification '{cls}' is not allowed in the HR knowledge base")
            if status not in ALLOWED_STATUSES:
                raise ValueError(f"status '{status}' unknown")
            source_type = str(meta.get("source_type") or "").strip().lower() or None
            if source_type and source_type not in SOURCE_TYPES:
                raise ValueError(f"source_type '{source_type}' unknown")
            right = index_right(meta)
            if status == "approved" and settings.is_live and is_placeholder(str(meta.get("approved_by") or "")):
                raise ValueError("APPROVAL_REQUIRED: approved_by names no real approver")
            if status == "approved" and right in ("review", "denied"):
                status = "reference"  # RIGHTS_UNKNOWN: the link and passport only, the text is not indexed
        except (ValueError, yaml.YAMLError) as exc:
            result.refused.append((path.name, str(exc)))
            logger.warning("Knowledge file {} refused: {}", path.name, exc)
            await audit.record(session, "knowledge.load", "refused", object_type="file", object_id=path.name[:60],
                               detail=str(exc))
            continue

        doc_id = str(meta["doc_id"]).strip()
        seen.add(doc_id)
        file_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        existing = await session.get(KnowledgeDocument, doc_id)
        if existing and existing.file_hash == file_hash:
            result.unchanged.append(doc_id)
            continue

        await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc_id))
        doc = existing or KnowledgeDocument(doc_id=doc_id)
        doc.title = str(meta["title"]).strip()
        doc.version = str(meta["version"]).strip()
        doc.status = status
        doc.classification = cls
        doc.roles = str(meta.get("roles") or "")
        doc.departments = str(meta.get("departments") or "")
        doc.effective_from = _as_date(meta.get("effective_from"))
        doc.effective_to = _as_date(meta.get("effective_to"))
        doc.owner = str(meta.get("owner") or "")
        doc.approved_by = str(meta.get("approved_by") or "")
        doc.review_at = _as_date(meta.get("review_at"))
        doc.language = str(meta.get("language") or "uz_cyrl")
        doc.file_hash = file_hash
        doc.source_type, doc.rights_index = source_type, right
        doc.publisher = str(meta.get("publisher") or "").strip()[:200] or None
        doc.canonical_url = str(meta.get("canonical_url") or "").strip()[:500] or None
        doc.jurisdiction = str(meta.get("jurisdiction") or "").strip()[:60] or None
        if existing is None:
            session.add(doc)
        await session.flush()
        if status == "reference":
            result.reference.append(doc_id)
            await audit.record(session, "knowledge.load", "reference_only", object_type="document",
                               object_id=doc_id, detail="RIGHTS_UNKNOWN")
            continue
        for position, (section, text) in enumerate(chunk_markdown(body)):
            session.add(KnowledgeChunk(
                doc_id=doc_id, doc_version=doc.version, position=position,
                section=section or doc.title, text=text, norm_text=normalize(f"{section} {text}"),
            ))
        result.loaded.append(doc_id)
        await audit.record(session, "knowledge.load", "loaded", object_type="document", object_id=doc_id,
                           detail=f"v{doc.version} {status}")

    stale = (await session.execute(
        select(KnowledgeDocument).where(KnowledgeDocument.status != "retired")
    )).scalars().all()
    for doc in stale:
        if doc.doc_id not in seen:
            doc.status = "retired"
            await session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.doc_id == doc.doc_id))
            result.retired.append(doc.doc_id)
            await audit.record(session, "knowledge.load", "retired", object_type="document", object_id=doc.doc_id)
    return result
