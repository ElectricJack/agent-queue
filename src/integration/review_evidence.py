"""Trusted reviewer verdicts for exact integration source snapshots."""

from __future__ import annotations

import math
import time
import uuid
from typing import Any

from sqlalchemy import and_, func, insert, select

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    archived_tasks,
    integration_review_evidence,
    task_branch_origins,
    task_dependencies,
    task_integration_checkpoints,
    task_session_attempts,
    tasks,
    projects,
)
from src.git.manager import RemoteRefState
from src.integration.epic_dependencies import dependents_of
from src.integration.settling import note_approval
from src.models import TaskStatus


_EVIDENCE_NAMESPACE = uuid.UUID("f22a352d-2664-4db5-ad67-6a60c9db863e")
_REVIEW_PROFILES = frozenset({"reviewer", "final-reviewer"})


class ReviewEvidenceProducer:
    """Resolve graph identity and pin one server-observed Git verdict."""

    def __init__(self, db, promotion_service, *, clock=time.time) -> None:
        self.db = db
        self.promotion = promotion_service
        self.clock = clock

    async def snapshot_from_pull_request(
        self,
        epic_task_id: str,
        *,
        verdict: str,
        reviewer_login: str,
        reviewed_sha: str,
        summary: str = "",
        feedback: str = "",
    ) -> dict[str, Any] | None:
        """Store a GitHub verdict against the exact verified train epic head."""
        if verdict not in {"approved", "rejected"}:
            raise ValueError(f"unsupported verdict: {verdict}")
        if not reviewer_login or not reviewer_login.strip():
            raise ValueError("reviewer_login is required")

        async with self.db._engine.connect() as conn:
            source = await self._pull_request_source_on(conn, epic_task_id)
        if source is None:
            return None
        if reviewed_sha != source["head"]:
            raise HierarchyError("stale_head", "reviewed head is not the verified epic head")

        resolved = await self.promotion._resolve_repository(source["repository_id"])
        if resolved.repo.project_id != source["project_id"]:
            raise HierarchyError("invalid", "review repository project changed")
        await self.promotion._ensure_retained_repository(resolved)
        async with self.promotion.git.arepository_transaction(str(resolved.retained_git_dir)):
            await self.promotion._fetch_all_heads(resolved.retained_git_dir, resolved.origin_url)
            remote = await self.promotion.git.als_remote_ref(
                str(resolved.retained_git_dir), source["branch"]
            )
            if remote.state is not RemoteRefState.PRESENT or remote.oid != reviewed_sha:
                raise HierarchyError("stale_head", "reviewed remote ref is not the exact head")
            tree = await self.promotion._tree_oid(resolved.retained_git_dir, reviewed_sha)

        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, source["project_id"])
            current = await self._pull_request_source_on(conn, epic_task_id)
            if current != source:
                raise HierarchyError("stale_head", "epic snapshot changed before verdict commit")
            identity = ":".join(
                (
                    "github",
                    epic_task_id,
                    source["repository_id"],
                    source["base"],
                    reviewed_sha,
                    str(source["generation"]),
                    verdict,
                    reviewer_login,
                    source["pr_url"],
                )
            )
            evidence_id = f"review-{uuid.uuid5(_EVIDENCE_NAMESPACE, identity)}"
            existing = (
                (
                    await conn.execute(
                        select(integration_review_evidence).where(
                            integration_review_evidence.c.id == evidence_id
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return dict(existing)
            latest_created_at = (
                await conn.execute(
                    select(func.max(integration_review_evidence.c.created_at)).where(
                        integration_review_evidence.c.source_task_id == epic_task_id,
                        integration_review_evidence.c.repository_id == source["repository_id"],
                        integration_review_evidence.c.source_base == source["base"],
                        integration_review_evidence.c.reviewed_head_sha == reviewed_sha,
                        integration_review_evidence.c.generation == source["generation"],
                    )
                )
            ).scalar_one()
            created_at = self.clock()
            if latest_created_at is not None:
                created_at = max(created_at, math.nextafter(latest_created_at, math.inf))
            evidence = {
                "id": evidence_id,
                "source_task_id": epic_task_id,
                "repository_id": source["repository_id"],
                "source_base": source["base"],
                "reviewed_head_sha": reviewed_sha,
                "reviewed_tree_sha": tree,
                "reviewer_task_id": None,
                "reviewer_session_attempt_id": None,
                "reviewer_identity": f"github:{reviewer_login}",
                "review_kind": "parent",
                "generation": source["generation"],
                "verdict": verdict,
                "evidence": {
                    "decision_path": "github_pull_request",
                    "summary": summary,
                    "feedback": feedback,
                    "reviewed_sha": reviewed_sha,
                    "pr_url": source["pr_url"],
                    "verification_id": source["verification_id"],
                },
                "created_at": created_at,
            }
            await self._append_on(conn, evidence)
            if verdict == "approved":
                await note_approval(conn, project_id=source["project_id"], now=created_at)
            else:
                for dependent_id in sorted(await dependents_of(conn, epic_task_id)):
                    await self.db.add_task_label(dependent_id, "needs-rebase", conn=conn)
            return evidence

    async def _pull_request_source_on(self, conn, epic_task_id: str) -> dict[str, Any] | None:
        checkpoint = task_integration_checkpoints
        origin = task_branch_origins
        row = (
            (
                await conn.execute(
                    select(
                        tasks.c.project_id,
                        tasks.c.branch_name.label("branch"),
                        tasks.c.pr_url,
                        tasks.c.repo_id.label("repository_id"),
                        checkpoint.c.checkpoint_sha,
                        checkpoint.c.verified_sha.label("head"),
                        checkpoint.c.verified_generation,
                        checkpoint.c.generation,
                        checkpoint.c.current_verification_id.label("verification_id"),
                        checkpoint.c.last_completed_verification_id,
                        origin.c.base_sha.label("base"),
                    )
                    .select_from(
                        tasks.join(projects, projects.c.id == tasks.c.project_id)
                        .join(
                            checkpoint,
                            and_(
                                checkpoint.c.task_id == tasks.c.id,
                                checkpoint.c.repository_id == tasks.c.repo_id,
                                checkpoint.c.branch == tasks.c.branch_name,
                            ),
                        )
                        .join(
                            origin,
                            and_(
                                origin.c.task_id == tasks.c.id,
                                origin.c.repository_id == tasks.c.repo_id,
                                origin.c.retired_at.is_(None),
                            ),
                        )
                    )
                    .where(
                        tasks.c.id == epic_task_id,
                        tasks.c.parent_task_id.is_(None),
                        tasks.c.status == TaskStatus.COMPLETED.value,
                        tasks.c.branch_name.is_not(None),
                        tasks.c.pr_url.is_not(None),
                        projects.c.hierarchical_integration_mode == "train",
                        projects.c.integration_repository_id == tasks.c.repo_id,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            row is None
            or not row["pr_url"].strip()
            or not row["branch"].strip()
            or row["head"] is None
            or row["head"] != row["checkpoint_sha"]
            or row["verified_generation"] != row["generation"]
            or not row["verification_id"]
            or row["verification_id"] != row["last_completed_verification_id"]
        ):
            return None
        active_child = (
            await conn.execute(
                select(tasks.c.id).where(tasks.c.parent_task_id == epic_task_id).limit(1)
            )
        ).first()
        archived_child = (
            await conn.execute(
                select(archived_tasks.c.id)
                .where(archived_tasks.c.parent_task_id == epic_task_id)
                .limit(1)
            )
        ).first()
        if active_child is None and archived_child is None:
            return None
        return {
            "project_id": row["project_id"],
            "repository_id": row["repository_id"],
            "branch": row["branch"],
            "pr_url": row["pr_url"],
            "base": row["base"],
            "head": row["head"],
            "generation": int(row["generation"]),
            "verification_id": row["verification_id"],
        }

    async def snapshot(
        self,
        review_task,
        session,
        *,
        verdict: str,
        summary: str = "",
        feedback: str = "",
        requested_subject_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Precompute immutable Git facts; return None for legacy projects."""
        if verdict not in {"approved", "rejected"}:
            raise HierarchyError("invalid", "review verdict is invalid")
        if (
            review_task.profile_id not in _REVIEW_PROFILES
            or session is None
            or session.task_id != review_task.id
            or session.project_id != review_task.project_id
            or session.profile_id != review_task.profile_id
            or session.agent_id != review_task.assigned_agent_id
            or session.state not in {"starting", "running"}
        ):
            raise HierarchyError("unauthorized", "reviewer session identity is not live")
        project = await self.db.get_project(review_task.project_id)
        if getattr(project, "hierarchical_integration_mode", "disabled") not in {
            "hierarchy",
            "train",
        }:
            return None
        subject, graph_link = await self._subject(review_task)
        if requested_subject_id is not None and requested_subject_id != subject.id:
            raise HierarchyError("unauthorized", "review target is not graph-derived")
        if subject.project_id != review_task.project_id:
            raise HierarchyError("unauthorized", "review subject belongs to another project")
        attempts = await self.db.list_task_session_attempts(
            review_task.id, project_id=review_task.project_id
        )
        if not attempts or attempts[0]["session_id"] != session.id:
            raise HierarchyError("unauthorized", "reviewer session attempt is not current")
        attempt = attempts[0]
        repository_id = getattr(project, "integration_repository_id", None)
        if not repository_id or subject.repo_id != repository_id or not subject.branch_name:
            raise HierarchyError("invalid", "review subject is not in the designated repository")
        origin = await self.db.get_task_branch_origin_for_promotion(subject.id, repository_id)
        checkpoint = await self.db.get_integration_checkpoint(subject.id)
        if origin is None or checkpoint is None:
            raise HierarchyError("invalid", "review subject has no exact integration snapshot")
        generation = int(checkpoint["generation"])
        children = await self.db.get_children(subject.id, limit=1)
        review_kind = "parent" if children else "leaf"
        if review_kind == "parent":
            if (
                checkpoint.get("verified_generation") != generation
                or not checkpoint.get("verified_sha")
            ):
                raise HierarchyError("invalid", "parent aggregate is not currently verified")
            head = checkpoint["verified_sha"]
            verification_id = checkpoint.get("current_verification_id")
        else:
            head = checkpoint["checkpoint_sha"]
            verification_id = None

        async with self.db._engine.connect() as conn:
            prior_evidence_id = (
                await conn.execute(
                    select(integration_review_evidence.c.id)
                    .where(
                        integration_review_evidence.c.source_task_id == subject.id,
                        integration_review_evidence.c.repository_id == repository_id,
                        integration_review_evidence.c.source_base == origin["base_sha"],
                        integration_review_evidence.c.reviewed_head_sha == head,
                        integration_review_evidence.c.generation == generation,
                    )
                    .order_by(
                        integration_review_evidence.c.created_at.desc(),
                        integration_review_evidence.c.id.desc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()

        resolved = await self.promotion._resolve_repository(repository_id)
        if resolved.repo.project_id != subject.project_id:
            raise HierarchyError("invalid", "review repository project changed")
        await self.promotion._ensure_retained_repository(resolved)
        async with self.promotion.git.arepository_transaction(str(resolved.retained_git_dir)):
            await self.promotion._fetch_all_heads(
                resolved.retained_git_dir, resolved.origin_url
            )
            remote = await self.promotion.git.als_remote_ref(
                str(resolved.retained_git_dir), subject.branch_name
            )
            if remote.state is not RemoteRefState.PRESENT or remote.oid != head:
                raise HierarchyError("stale_head", "reviewed remote ref is not the exact head")
            tree = await self.promotion._tree_oid(resolved.retained_git_dir, head)

        identity = ":".join(
            (
                review_task.id,
                attempt["id"],
                verdict,
                subject.id,
                repository_id,
                origin["base_sha"],
                head,
                str(generation),
            )
        )
        return {
            "id": f"review-{uuid.uuid5(_EVIDENCE_NAMESPACE, identity)}",
            "source_task_id": subject.id,
            "repository_id": repository_id,
            "source_base": origin["base_sha"],
            "reviewed_head_sha": head,
            "reviewed_tree_sha": tree,
            "reviewer_task_id": review_task.id,
            "reviewer_session_attempt_id": attempt["id"],
            "review_kind": review_kind,
            "generation": generation,
            "verdict": verdict,
            "evidence": {
                "decision_path": "review_task_close" if verdict == "approved" else "reopen_with_feedback",
                "graph_link": graph_link,
                "summary": summary,
                "feedback": feedback,
                "verification_id": verification_id,
                "prior_evidence_id": prior_evidence_id,
            },
            "created_at": self.clock(),
        }

    async def complete_review_on(self, conn, review_task_id: str, evidence: dict, **transition):
        rejected = (
            await conn.execute(
                select(integration_review_evidence.c.id).where(
                    integration_review_evidence.c.reviewer_task_id == review_task_id,
                    integration_review_evidence.c.reviewer_session_attempt_id
                    == evidence["reviewer_session_attempt_id"],
                    integration_review_evidence.c.verdict == "rejected",
                )
            )
        ).first()
        if rejected:
            if evidence["reviewer_task_id"] != review_task_id:
                raise HierarchyError("unauthorized", "review evidence task identity changed")
        else:
            await self._revalidate_on(conn, review_task_id, evidence)
            await self._append_on(conn, evidence)
        return await self.db._apply_transition(
            conn, review_task_id, TaskStatus.COMPLETED, **transition
        )

    async def reject_and_reopen_on(
        self, conn, subject_task_id: str, review_task_id: str, evidence: dict, **transition
    ):
        await self._revalidate_on(conn, review_task_id, evidence)
        if evidence["source_task_id"] != subject_task_id or evidence["verdict"] != "rejected":
            raise HierarchyError("invalid", "rejection subject changed")
        await self._append_on(conn, evidence)
        return await self.db._apply_transition(
            conn, subject_task_id, TaskStatus.READY, **transition
        )

    async def _append_on(self, conn, evidence: dict) -> None:
        existing = (
            await conn.execute(
                select(integration_review_evidence).where(
                    integration_review_evidence.c.id == evidence["id"]
                )
            )
        ).mappings().one_or_none()
        if existing is None:
            await conn.execute(insert(integration_review_evidence).values(**evidence))
        elif any(existing[key] != value for key, value in evidence.items() if key != "created_at"):
            raise HierarchyError("invariant_error", "review evidence identity changed")

    async def _revalidate_on(self, conn, review_task_id: str, evidence: dict) -> None:
        review = (
            await conn.execute(select(tasks).where(tasks.c.id == review_task_id))
        ).mappings().one_or_none()
        if review is None:
            raise HierarchyError("stale_head", "review task disappeared before verdict commit")
        await self.db.lock_hierarchy_project(conn, review["project_id"])
        latest_attempt = (
            await conn.execute(
                select(task_session_attempts)
                .where(task_session_attempts.c.task_id == review_task_id)
                .order_by(
                    task_session_attempts.c.started_at.desc(),
                    task_session_attempts.c.id.desc(),
                )
                .limit(1)
            )
        ).mappings().one_or_none()
        origin = (
            await conn.execute(
                select(task_branch_origins).where(
                    task_branch_origins.c.task_id == evidence["source_task_id"],
                    task_branch_origins.c.repository_id == evidence["repository_id"],
                    task_branch_origins.c.base_sha == evidence["source_base"],
                    task_branch_origins.c.retired_at.is_(None),
                )
            )
        ).first()
        checkpoint = (
            await conn.execute(
                select(task_integration_checkpoints).where(
                    task_integration_checkpoints.c.task_id == evidence["source_task_id"],
                    task_integration_checkpoints.c.generation == evidence["generation"],
                )
            )
        ).mappings().one_or_none()
        source = (
            await conn.execute(
                select(tasks.c.status).where(tasks.c.id == evidence["source_task_id"])
            )
        ).mappings().one_or_none()
        latest_evidence_id = (
            await conn.execute(
                select(integration_review_evidence.c.id)
                .where(
                    integration_review_evidence.c.source_task_id
                    == evidence["source_task_id"],
                    integration_review_evidence.c.repository_id == evidence["repository_id"],
                    integration_review_evidence.c.source_base == evidence["source_base"],
                    integration_review_evidence.c.reviewed_head_sha
                    == evidence["reviewed_head_sha"],
                    integration_review_evidence.c.generation == evidence["generation"],
                )
                .order_by(
                    integration_review_evidence.c.created_at.desc(),
                    integration_review_evidence.c.id.desc(),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        edges = (
            await conn.execute(
                select(task_dependencies.c.depends_on_task_id, task_dependencies.c.dep_type).where(
                    task_dependencies.c.task_id == review_task_id
                )
            )
        ).all()
        if evidence["evidence"]["graph_link"] == "discovered-from":
            subjects = {row[0] for row in edges if row[1] == "discovered-from"}
            linked = subjects == {evidence["source_task_id"]}
        else:
            review_ids = [row[0] for row in edges if row[1] == "blocks"]
            final_links = (
                await conn.execute(
                    select(task_dependencies.c.depends_on_task_id).where(
                        task_dependencies.c.task_id.in_(review_ids),
                        task_dependencies.c.dep_type == "discovered-from",
                    )
                )
            ).scalars().all() if review_ids else []
            candidate_ids: set[str] = set()
            if final_links:
                candidates = (
                    await conn.execute(
                        select(tasks.c.id, projects.c.hierarchical_integration_mode)
                        .join(projects, projects.c.id == tasks.c.project_id)
                        .where(tasks.c.id.in_(final_links))
                    )
                ).all()
                candidate_ids = {
                    row[0]
                    for row in candidates
                    if row[1] in {"hierarchy", "train"}
                }
            linked = candidate_ids == {evidence["source_task_id"]}
        expected_head = (
            checkpoint.get("verified_sha")
            if checkpoint and evidence["review_kind"] == "parent"
            else checkpoint.get("checkpoint_sha") if checkpoint else None
        )
        parent_generation_current = (
            evidence["review_kind"] != "parent"
            or checkpoint.get("verified_generation") == evidence["generation"]
        ) if checkpoint else False
        if (
            evidence["reviewer_task_id"] != review_task_id
            or review["profile_id"] not in _REVIEW_PROFILES
            or review["status"] != TaskStatus.IN_PROGRESS.value
            or latest_attempt is None
            or latest_attempt["id"] != evidence["reviewer_session_attempt_id"]
            or latest_attempt["profile_id"] != review["profile_id"]
            or latest_attempt["agent_id"] != review["assigned_agent_id"]
            or latest_attempt["project_id"] != review["project_id"]
            or latest_attempt["state"] not in {"starting", "running"}
            or origin is None
            or checkpoint is None
            or source is None
            or source["status"] != TaskStatus.COMPLETED.value
            or latest_evidence_id != evidence["evidence"].get("prior_evidence_id")
            or not linked
            or not parent_generation_current
            or expected_head != evidence["reviewed_head_sha"]
        ):
            raise HierarchyError("stale_head", "review snapshot changed before verdict commit")

    async def _subject(self, review_task):
        edges = await self.db.get_typed_dependencies(review_task.id)
        if review_task.profile_id == "reviewer":
            subjects = {task_id for task_id, kind in edges if kind == "discovered-from"}
            if len(subjects) != 1:
                raise HierarchyError("unauthorized", "reviewer has no unique graph subject")
            subject = await self.db.get_task(next(iter(subjects)))
            if subject is None:
                raise HierarchyError("invalid", "review subject is missing")
            return subject, "discovered-from"

        review_ids = {task_id for task_id, kind in edges if kind == "blocks"}
        candidates = []
        for review_id in sorted(review_ids):
            links = await self.db.get_typed_dependencies(review_id)
            subjects = {task_id for task_id, kind in links if kind == "discovered-from"}
            if len(subjects) != 1:
                raise HierarchyError("unauthorized", "final review graph is ambiguous")
            subject = await self.db.get_task(next(iter(subjects)))
            if subject is None:
                raise HierarchyError("invalid", "final review subject is missing")
            project = await self.db.get_project(subject.project_id)
            if getattr(project, "hierarchical_integration_mode", "disabled") in {
                "hierarchy",
                "train",
            }:
                candidates.append(subject)
        unique = {task.id: task for task in candidates}
        if len(unique) != 1:
            raise HierarchyError("unauthorized", "final review has no unique integration subject")
        return next(iter(unique.values())), "final-review-blocks"
