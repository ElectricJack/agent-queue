"""Parent policy facts retained for read-only observation and policy validation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from src.integration.models import Fence
from src.integration.parent_subjects import ParentSubjectFacts
from src.integration.runtime_contracts import (
    SHA_PATTERN,
    MemberRef,
)

#: Marker naming a reopen the existing episode cannot satisfy until a human
#: clears the blocker (an ambiguous verifier subject, a manual hold, a dirty
#: checkout). Retained for historical records: the observer reports it to the
#: policy, which asks a human instead of backing off on every visit.
REOPEN_REFUSED_META_KEY = "integration_reopen_refused"

#: Reopen refusals no retry can satisfy: the verifier subject is ambiguous, or a
#: manual hold, a missing stop proof or a dirty checkout blocks the reopen.
#: ``changed`` and the other refusals stay an unknown answer, because
#: re-diagnosing can still succeed and a false human gate is worse than a retry.
DURABLE_REOPEN_REFUSALS = frozenset({"ambiguous", "blocked"})


class PendingParentPublication(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    intent_id: str
    expected_old_sha: str = Field(pattern=SHA_PATTERN)
    new_sha: str = Field(pattern=SHA_PATTERN)
    fence: Fence


class ParentPolicyFacts(ParentSubjectFacts):
    """Typed additions to the parent observer; all participate in its digest."""

    identity_moved: bool = False
    competing_lease: bool = False
    failed_child_count: int = 0
    collection_members: tuple[MemberRef, ...] = ()
    collector_fence: Fence | None = None
    pending_publication: PendingParentPublication | None = None
    legacy_repair_dossier: dict | None = None
    verifier_task_id: str | None = None
    verifier_failed: bool = False
    reopen_refused: bool = False
    aggregate_verified: bool = False
    parent_completed: bool = False
    ci_evidence_ids: tuple[str, ...] = ()
    writer_file_ordinal: int = Field(default=1, ge=1)
