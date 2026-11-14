"""The source registry (spec 2.0 §5, §7, §29): which outside sources HARI may know about, and on what terms.

`knowledge/sources/registry.yaml` lists each source with its passport. An entry is "reference" (title
and link only) until a person records the rights and approves it. This module only reads and checks
that file; it fetches nothing from the internet.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from hari.digest import is_placeholder

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "knowledge" / "sources" / "registry.yaml"
STATUSES = {"reference", "quarantine", "active", "superseded", "suspended"}
REVIEWS = {"daily", "monthly", "quarterly", "on_use"}
REQUIRED = ("source_id", "title", "publisher", "kind", "canonical_url", "jurisdiction", "language", "review")


@dataclass(frozen=True)
class Source:
    source_id: str
    title: str
    publisher: str
    kind: str
    canonical_url: str
    jurisdiction: str
    language: str
    review: str
    status: str
    may_index: bool
    approved_by: str
    note: str

    @property
    def usable(self) -> bool:
        """May its text be loaded into the knowledge base?"""
        return self.status == "active" and self.may_index


def _check(entry: dict) -> Source:
    sid = str(entry.get("source_id") or "?")
    missing = [k for k in REQUIRED if not str(entry.get(k) or "").strip()]
    if missing:
        raise ValueError(f"{sid}: missing {', '.join(missing)}")
    url = urlparse(str(entry["canonical_url"]))
    if url.scheme != "https" or not url.hostname:
        raise ValueError(f"{sid}: canonical_url must be an https address")
    if entry["review"] not in REVIEWS:
        raise ValueError(f"{sid}: review '{entry['review']}' unknown")
    status = str(entry.get("status") or "reference")
    if status not in STATUSES:
        raise ValueError(f"{sid}: status '{status}' unknown")
    rights = entry.get("rights") or {}
    if not isinstance(rights, dict):
        raise ValueError(f"{sid}: rights must be a mapping")
    if str(rights.get("train", "false")).lower() not in ("false", "no", "0"):
        raise ValueError(f"{sid}: rights.train must be false")
    may_index = str(rights.get("index") or "review").lower() in ("allowed", "true", "yes")
    approved_by = str(entry.get("approved_by") or "")
    if status == "active":
        if not may_index:
            raise ValueError(f"{sid}: RIGHTS_UNKNOWN — active needs rights.index: allowed")
        if is_placeholder(approved_by):
            raise ValueError(f"{sid}: APPROVAL_REQUIRED — active needs a real approved_by")
    return Source(sid, str(entry["title"]), str(entry["publisher"]), str(entry["kind"]), str(entry["canonical_url"]),
                  str(entry["jurisdiction"]), str(entry["language"]), str(entry["review"]), status, may_index,
                  approved_by, str(entry.get("note") or ""))


def load(path: str | Path | None = None) -> dict[str, Source]:
    """All sources by id. Raises ValueError naming the first entry that breaks a rule."""
    data = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8")) or {}
    entries = data.get("sources")
    if not isinstance(entries, list) or not entries:
        raise ValueError("registry has no sources")
    out: dict[str, Source] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("a registry entry is not a mapping")
        source = _check(entry)
        if source.source_id in out:
            raise ValueError(f"{source.source_id}: listed twice")
        out[source.source_id] = source
    return out
