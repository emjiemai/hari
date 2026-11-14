"""One-time approvals bound to one exact action (spec 2.0 §25 "Approval token"; T19, T20).

An approval names the person, the action type, the subject, a hash of the payload and the version of
the record it was made against, and runs out after a few minutes. It works once. If anything in the
payload or the record changed since, it does not work: a new approval is needed. Silence is never
approval — nothing happens until `consume` succeeds.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from hari import audit
from hari.config import settings
from hari.db.models import Approval, Employee, as_utc
from hari.worktime import now_utc


class ApprovalError(Exception):
    """`code` is one of the spec's error codes; `reason` says which check failed."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code, self.reason = code, reason


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


async def request(session: AsyncSession, actor: Employee, action_type: str, subject_id: str, payload: dict,
                  source_version: str = "", now: datetime | None = None) -> Approval:
    now = now or now_utc()
    approval = Approval(
        nonce=secrets.token_hex(16), actor_id=actor.id, action_type=action_type, subject_id=subject_id,
        payload_hash=payload_hash(payload), source_version=source_version,
        expires_at=now + timedelta(minutes=settings.approval_ttl_minutes),
    )
    session.add(approval)
    await session.flush()
    await audit.record(session, "approval.request", "awaiting_approval", actor_id=actor.id, object_type=action_type,
                       object_id=subject_id, detail=f"approval={approval.id}")
    return approval


async def consume(session: AsyncSession, approval_id: str, actor: Employee, action_type: str, subject_id: str,
                  payload: dict, source_version: str = "", now: datetime | None = None) -> Approval:
    """Use the approval for exactly this action, or raise. The caller does the action only after this returns."""
    now = now or now_utc()
    approval = await session.get(Approval, approval_id)

    async def refuse(code: str, reason: str) -> ApprovalError:
        await audit.record(session, "approval.consume", reason, actor_id=actor.id, object_type=action_type,
                           object_id=subject_id, detail=f"approval={approval_id} {code}")
        return ApprovalError(code, reason)

    if approval is None:
        raise await refuse("NEEDS_APPROVAL", "missing")
    if approval.actor_id != actor.id:
        raise await refuse("FORBIDDEN", "other_actor")
    if approval.action_type != action_type or approval.subject_id != subject_id:
        raise await refuse("CONFLICT", "other_action")
    if approval.used_at is not None:
        raise await refuse("CONFLICT", "already_used")
    if as_utc(approval.expires_at) <= now:
        raise await refuse("NEEDS_APPROVAL", "expired")
    if approval.payload_hash != payload_hash(payload):
        raise await refuse("CONFLICT", "payload_changed")
    if approval.source_version != source_version:
        raise await refuse("CONFLICT", "version_changed")
    # One guarded UPDATE: of two presses at the same moment, only the first uses the nonce.
    used = await session.execute(
        update(Approval).where(Approval.id == approval_id, Approval.used_at.is_(None)).values(used_at=now)
        .execution_options(synchronize_session=False))
    if used.rowcount != 1:
        raise await refuse("CONFLICT", "already_used")
    await session.refresh(approval)
    await audit.record(session, "approval.consume", "approved", actor_id=actor.id, object_type=action_type,
                       object_id=subject_id, detail=f"approval={approval_id}")
    return approval
