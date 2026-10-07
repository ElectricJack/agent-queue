"""Prove, record and route a train source whose recorded base is not its ancestor.

A train member merges ``source_base..reviewed_head`` onto the candidate, so the
recorded origin base is frozen provenance.  A head that does not descend from it
(a worker stacked its branch on another line and dropped the origin) can never be
constructed.  Admission refuses such an identity before it is approved, and
construction withdraws one that was approved earlier; both record the same exact
rejection and route the source to a repair that keeps its reviewed history.
See docs/superpowers/specs/2026-10-01-invalid-source-ancestry-recovery-design.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from src.database.queries.hierarchy_queries import HierarchyError
from src.git.manager import GitError, is_valid_git_oid

ANCESTRY_REVIEWER = "integration:source-ancestry"
ANCESTRY_DECISION_PATH = "source_ancestry_invalid"
#: Failures Git proves from objects that are present.  Both are permanent for an
#: exact source identity; a missing object is not proof and is never withdrawn.
PROVEN_REASONS = frozenset({"source_base_not_ancestor", "reviewed_tree_mismatch"})

_ANCESTRY_NAMESPACE = uuid.UUID("5b0f2d6e-6f8a-4f53-9a4e-2f6f0c1b7d31")


@dataclass(frozen=True)
class SourceAncestryObservation:
    """Server-observed proof that one exact train source identity is unbuildable.

    ``source`` carries ``project_id``, ``repository_id``, ``branch``, ``pr_url``,
    ``base``, ``head``, ``generation``, ``review_kind`` and ``verification_id``,
    the shape ``ReviewEvidenceProducer._pull_request_source_on`` returns.
    """

    task_id: str
    source: dict[str, Any]
    reviewed_tree_sha: str
    reason: str
    merge_base: str | None = None
    detected_by: str = "admission"
    batch_id: str | None = None
    claimed_tree_sha: str | None = None

    @classmethod
    def from_evidence(
        cls, task_id: str, source: dict[str, Any], row: dict[str, Any]
    ) -> SourceAncestryObservation:
        """Rebuild the observation a recorded ancestry rejection was written from."""
        detail = row["evidence"] or {}
        return cls(
            task_id=task_id,
            source=dict(source),
            reviewed_tree_sha=row["reviewed_tree_sha"],
            reason=detail.get("reason") or "source_base_not_ancestor",
            merge_base=detail.get("merge_base"),
            detected_by=detail.get("detected_by") or "admission",
            batch_id=detail.get("batch_id"),
            claimed_tree_sha=detail.get("claimed_tree_sha"),
        )

    def identity(self) -> tuple[str, str, str, str, int]:
        return (
            self.task_id,
            self.source["repository_id"],
            self.source["base"],
            self.source["head"],
            int(self.source["generation"]),
        )

    def matches(self, source: dict[str, Any] | None) -> bool:
        return source is not None and (
            source["repository_id"],
            source["base"],
            source["head"],
            int(source["generation"]),
        ) == self.identity()[1:]


class SourceAncestryInvalid(HierarchyError):
    """Approval refused: Git proved the source's recorded base is not its ancestor."""

    def __init__(self, observation: SourceAncestryObservation) -> None:
        self.observation = observation
        super().__init__(
            "invalid_ancestry",
            describe(observation),
            context={
                "task_id": observation.task_id,
                "source_base": observation.source["base"],
                "reviewed_head_sha": observation.source["head"],
                "merge_base": observation.merge_base,
                "reason": observation.reason,
            },
        )


def describe(observation: SourceAncestryObservation) -> str:
    source = observation.source
    if observation.reason == "reviewed_tree_mismatch":
        return (
            f"source {observation.task_id} head {source['head']} has tree "
            f"{observation.reviewed_tree_sha}, not the recorded tree "
            f"{observation.claimed_tree_sha}"
        )
    merge_base = observation.merge_base or "none"
    return (
        f"source {observation.task_id} recorded base {source['base']} is not an ancestor "
        f"of its head {source['head']} on {source.get('branch') or '(unknown branch)'} "
        f"(merge-base {merge_base})"
    )


def repair_feedback(observation: SourceAncestryObservation) -> str:
    """Instructions a worker can act on without rewriting reviewed history."""
    source = observation.source
    branch = source.get("branch") or "the task branch"
    found = (
        f"in batch {observation.batch_id}" if observation.batch_id else "at admission"
    )
    return (
        f"Integration withdrew this source {found}: {describe(observation)}. The train "
        "merges `recorded base..head` onto main, so a head that does not descend from its "
        "recorded base can never be built; the history on this branch dropped the base it "
        "was created from.\n\n"
        f"Repair on {branch} without discarding the reviewed commits: fetch origin and merge "
        f"the recorded base `{source['base']}` into the branch (`git merge {source['base']}`; "
        "merging the current default branch also works, because it contains the base). "
        "Do not rebase, squash away or force-push the existing history. Resolve conflicts, "
        "regenerate generated files, re-run the focused checks with `aq test`, publish with "
        "`aq git push` and close again; the new head is a new source identity and is "
        "admitted normally once `merge-base --is-ancestor` holds.\n"
        f"Withdrawn identity: base {source['base']}, head {source['head']}, generation "
        f"{source['generation']}, PR {source.get('pr_url') or '(none)'}, evidence "
        f"{rejection_evidence_id(observation)}."
    )


def rejection_evidence_id(observation: SourceAncestryObservation) -> str:
    identity = ":".join(str(part) for part in (*observation.identity(), observation.reason))
    return f"review-{uuid.uuid5(_ANCESTRY_NAMESPACE, identity)}"


async def prove_member_identity(git, store: str, member: dict[str, Any]):
    """Return ``(reason, merge_base, tree)`` for one frozen batch member.

    ``reason`` is ``None`` when the identity holds.  ``source_base_missing``,
    ``reviewed_head_missing`` and a failed probe (``ancestry_unknown``) are not
    proof; the others are in :data:`PROVEN_REASONS`.
    """
    base, head = member["source_base_sha"], member["reviewed_head_sha"]
    for label, oid in (("source_base_missing", base), ("reviewed_head_missing", head)):
        exists = await git.arun_git_result(["cat-file", "-e", f"{oid}^{{commit}}"], cwd=store)
        if exists.returncode != 0:
            return label, None, None
    tree_result = await git.arun_git_result(["rev-parse", f"{head}^{{tree}}"], cwd=store)
    tree = tree_result.stdout.strip() if tree_result.returncode == 0 else None
    ancestor = await git.ais_ancestor(store, base, head, strict=True)
    if ancestor is None:
        return "ancestry_unknown", None, tree
    if not ancestor:
        return "source_base_not_ancestor", await merge_base_of(git, store, base, head), tree
    if tree != member["reviewed_tree_sha"]:
        return "reviewed_tree_mismatch", None, tree
    return None, None, tree


async def merge_base_of(git, store: str, base: str, head: str) -> str | None:
    result = await git.arun_git_result(["merge-base", base, head], cwd=store)
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


async def effective_source_base(
    git, store: str, recorded_base: str, current: str, head: str
) -> str:
    """Avoid replaying inherited target changes without widening a source delta.

    Callers retain the recorded-base identity and check reserved paths in both
    recorded and effective deltas. Advance only to a unique common ancestor
    that contains that base and lies on the target's first-parent chain. A
    second-parent ancestor may have supplied history without its content, so
    it cannot replace the recorded delta. Failed probes raise rather than
    turning an unavailable ancestry proof into a different merge input.
    """
    result = await git.arun_git_result(
        ["--no-replace-objects", "merge-base", "--all", current, head], cwd=store
    )
    if result.returncode == 1 and not result.stdout.strip():
        return recorded_base
    if result.returncode:
        raise GitError(result.stderr or "could not determine effective source base")
    bases = result.stdout.split()
    if not bases or any(not is_valid_git_oid(base) for base in bases):
        raise GitError("merge-base did not produce commit OIDs")
    if len(bases) != 1:
        return recorded_base
    natural = bases[0]
    if natural == recorded_base:
        return recorded_base
    for ancestor, descendant in ((recorded_base, natural), (natural, current), (natural, head)):
        proof = await git.arun_git_result(
            ["--no-replace-objects", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=store,
        )
        if proof.returncode == 1:
            return recorded_base
        if proof.returncode:
            raise GitError(proof.stderr or "effective source base ancestry probe failed")
    first_parents = await git.arun_git_result(
        ["--no-replace-objects", "rev-list", "--first-parent", f"{recorded_base}..{current}"],
        cwd=store,
    )
    if first_parents.returncode:
        raise GitError(first_parents.stderr or "effective source base first-parent probe failed")
    if natural not in first_parents.stdout.split():
        return recorded_base
    return natural
