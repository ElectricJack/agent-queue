"""Temporary git-first comparisons inside visits owned by the reconciler.

These logs are diagnostics, never policy inputs or cutover certification. The
old subject protocol remains authoritative. This adapter has only SELECT and
GitTruth read ports: no repair allocation, publication, journal or action port.
The plan fixes this module's allowance at 300 lines; stage four deletes it.
"""

from __future__ import annotations

import logging

from src.git.manager import is_valid_git_oid
from src.logging_config import CorrelationContext
from src.integration.delivery_truth import DeliveryRequest, DeliveryState
from src.integration.git_truth import GitTruth, epic_complete, repair_progress
from src.integration.observe import DatabaseObservationReader
from src.integration.subjects import CIState, SubjectEngine, SubjectKind

logger = logging.getLogger(__name__)


def _ref(value):
    return value if value.startswith("refs/heads/") else "refs/heads/" + value


class GitFirstDiagnostics:
    def __init__(self, db, git, reader):
        self.db, self.truth, self.reader = db, GitTruth(git), reader

    @staticmethod
    def record(subject, family, old, proposed, *, reason, **evidence):
        classification = (
            "unknown"
            if old is None or proposed is None
            else "agreement"
            if old == proposed
            else "disagreement"
        )
        evidence = {
            "subject_id": subject.id,
            "subject_version": subject.version,
            "project_id": subject.project_id,
            "repository_id": subject.repository_id,
            "family": family,
            "classification": classification,
            "authoritative": "subject_protocol",
            "old": old,
            "proposed": proposed,
            "reason": reason,
            **evidence,
        }
        # The daemon's stdlib bridge carries bound context into JSONL. Keep
        # the LogRecord extra too for consumers without that formatter.
        with CorrelationContext(git_first=evidence):
            logger.log(
                logging.INFO if classification == "agreement" else logging.WARNING,
                "integration git-first %s %s",
                family,
                classification,
                extra={"git_first": evidence},
            )

    async def __call__(self, subject, facts):
        if subject.engine is not SubjectEngine.RECONCILER:
            return
        snapshot = await self.reader.read(subject.id)
        if snapshot is None or snapshot.subject != subject:
            self.record(subject, "observation", None, None, reason="subject_changed")
            return
        # Lease eligibility comes from non-Git state even when fetch is unavailable.
        self.lease(subject, facts, snapshot)
        target = facts.tested_head
        target_ref = (
            target.ref
            if target
            else (subject.target_ref or _ref(snapshot.repository["default_branch"]))
        )
        store = snapshot.repository.get("checkout_base_path")
        fetched = None
        if store:
            fetched = await self.truth.snapshot(
                store,
                project_id=subject.project_id,
                repository_id=subject.repository_id,
                repository_url=snapshot.repository["url"],
                target_ref=target_ref,
            )
        children = {child.task_id: child for child in getattr(facts, "children", ())}
        tasks = {row["id"]: row for row in snapshot.all("tasks")}
        requests, bases = [], {}
        for member in facts.members:
            child = children.get(member.task_id)
            old = (
                child.selected_receipt_id is not None
                if child
                else None
                if member.ancestry == "unknown"
                else member.ancestry == "contained"
            )
            task = tasks.get(member.task_id)
            proof = None
            if task is not None and fetched is not None:
                completion = await self.db.get_task_completion(member.task_id)
                request = DeliveryRequest.from_task(
                    task,
                    completion,
                    repository_id=subject.repository_id,
                    target_ref=target_ref,
                )
                requests.append(request)
                if member.base_sha:
                    bases[member.task_id] = member.base_sha
                proof = await fetched.is_delivered(request, source_base=member.base_sha)
            proposed = (
                None
                if proof is None or proof.state is DeliveryState.UNKNOWN
                else proof.state in {
                    DeliveryState.CONTAINED, DeliveryState.NO_CHANGE, DeliveryState.NO_ARTIFACT,
                }
            )
            self.record(
                subject,
                "child_delivery",
                old,
                proposed,
                reason=proof.reason if proof else "source_or_checkout_unavailable",
                task_id=member.task_id,
                target_ref=target_ref,
                target_oid=fetched.target_oid if fetched else None,
                source_oid=proof.source_oid if proof else None,
                source_base=member.base_sha,
                old_source_oid=member.head_sha,
                old_target_oid=target.sha if target else None,
            )
        if subject.kind is SubjectKind.PARENT_EPISODE:
            await self.epic(subject, facts, snapshot, fetched, requests, bases)
        self.repair(subject, facts, snapshot, fetched)

    async def epic(self, subject, facts, snapshot, fetched, requests, bases):
        verification = getattr(facts, "verification", None)
        old = (
            None
            if getattr(facts, "readiness", "unknown") == "unknown"
            else facts.readiness == "ready"
            and verification is not None
            and verification.status == "passed"
        )
        proposed, reason = None, "git_or_children_unavailable"
        if fetched is not None and len(requests) == len(facts.members):
            # The legacy evidence producer owns authorization. Only its exact
            # approved/rejected tree verdicts can stand in for the future tree port.
            reviewed = [
                row
                for row in snapshot.all("integration_review_evidence")
                if row["source_task_id"] == subject.task_id
            ]
            approved_tree = None
            if reviewed and fetched.target_oid:
                tree = await fetched.truth.git.atree_sha(
                    fetched.observation.store,
                    fetched.target_oid,
                )
                reviewed = [row for row in reviewed if row["reviewed_tree_sha"] == tree]
            if reviewed:
                latest = max(reviewed, key=lambda row: (row["created_at"], row["id"]))
                if latest["verdict"] == "approved":
                    approved_tree = latest["reviewed_tree_sha"]
            green = (
                fetched.target_oid if facts.ci_for(fetched.target_oid) is CIState.GREEN else None
            )
            operation = next(
                (
                    row
                    for row in snapshot.all("integration_repair_operations")
                    if row.get("episode_id") == subject.parent_episode_id
                ),
                {},
            )
            admission = (operation.get("policy_snapshot") or {}).get("parent", {}).get("admission")
            if admission in {"reviewed", "authorized"}:
                proposed = await epic_complete(
                    fetched,
                    requests,
                    green_oid=green,
                    approved_tree=approved_tree,
                    review_required=admission == "reviewed",
                    held=bool(facts.holds),
                    source_bases=bases,
                )
                reason = "children_exact_head_checks_tree_review_and_holds"
                if not facts.holds and facts.ci_for(fetched.target_oid) in {
                    CIState.NONE,
                    CIState.INFRA,
                    CIState.UNTRUSTED,
                }:
                    proposed, reason = None, "exact_head_checks_unavailable"
            else:
                reason = "pinned_review_requirement_unavailable"
        self.record(
            subject,
            "epic_readiness",
            old,
            proposed,
            reason=reason,
            target_ref=subject.target_ref,
            target_oid=fetched.target_oid if fetched else None,
        )

    def repair(self, subject, facts, snapshot, fetched):
        stages = [
            row
            for row in snapshot.all("integration_repair_stages")
            if row.get("repair_task_id") == facts.writer.task_id and facts.writer.task_id
        ]
        if not stages:
            return
        stage = max(stages, key=lambda row: (row["ordinal"], row["created_at"]))
        head = fetched.target_oid if fetched else None
        green = bool(head and facts.ci_for(head) is CIState.GREEN)
        available = green or (
            is_valid_git_oid(stage.get("starting_sha")) and is_valid_git_oid(head)
        )
        self.record(
            subject,
            "repair_progress",
            not facts.no_progress,
            repair_progress(stage.get("starting_sha"), head, green=green) if available else None,
            reason="exact_head_green_or_moved" if available else "repair_git_unavailable",
            start_oid=stage.get("starting_sha"),
            head_oid=head,
            green=green,
            stored_no_progress=facts.no_progress,
            old_guard="stored_no_progress",
        )

    def lease(self, subject, facts, snapshot):
        target_ref = facts.tested_head.ref if facts.tested_head else subject.target_ref
        if target_ref is None:
            target_ref = _ref(snapshot.repository["default_branch"])
        owners = [
            row
            for row in snapshot.all("integration_branch_owners")
            if row["repository_id"] == subject.repository_id and _ref(row["ref"]) == target_ref
        ]
        owner = owners[0] if len(owners) == 1 else None
        operation = next(
            (
                row
                for row in snapshot.all("integration_repair_operations")
                if (
                    subject.parent_episode_id and row.get("episode_id") == subject.parent_episode_id
                )
                or (subject.batch_id and row.get("batch_id") == subject.batch_id)
            ),
            {},
        )
        requester = operation.get("id") or "root-reconciler:" + subject.repository_id
        old = (
            None
            if len(owners) > 1
            else (
                owner is None
                or owner["handoff_state"] == "released"
                or owner["owner_id"] == requester
                and owner["handoff_state"] != "handoff_pending"
            )
        )
        expires = owner.get("expires_at") if owner else None
        proposed = (
            None
            if len(owners) > 1
            else True
            if owner is None or owner["handoff_state"] == "released"
            else None
            if expires is None
            else expires <= facts.observed_at or owner["owner_id"] == requester
        )
        self.record(
            subject,
            "lease_eligibility",
            old,
            proposed,
            reason="non_git_lease" if proposed is not None else "lease_expiry_unavailable",
            input_kind="non_git",
            target_ref=target_ref,
            requester=requester,
            holder=owner["owner_id"] if owner else None,
            fence=owner["fence_token"] if owner else None,
            expires_at=expires,
            observed_at=facts.observed_at,
        )


def diagnostics_for(config, db, git, *, reader=None):
    """The selector never constructs a loop, transfers ownership or changes policy."""
    if getattr(config, "git_first", "shadow") != "shadow":
        return None
    return GitFirstDiagnostics(db, git, reader or DatabaseObservationReader(db))
