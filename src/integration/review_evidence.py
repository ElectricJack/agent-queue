"""Trusted reviewer verdicts for exact integration source snapshots."""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import insert, select

from src.database.queries.hierarchy_queries import HierarchyError
from src.database.tables import (
    integration_review_evidence,
    task_branch_origins,
    task_dependencies,
    task_integration_checkpoints,
    task_session_attempts,
    tasks,
    projects,
)
from src.git.manager import RemoteRefState
from src.models import TaskStatus


_EVIDENCE_NAMESPACE = uuid.UUID("f22a352d-2664-4db5-ad67-6a60c9db863e")
_REVIEW_PROFILES = frozenset({"reviewer", "final-reviewer"})


class ReviewEvidenceProducer:
    """Resolve graph identity and pin one server-observed Git verdict."""

    def __init__(self, db, promotion_service, *, clock=time.time) -> None:
        self.db = db
        self.promotion = promotion_service
        self.clock = clock

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
            await self.promotion._fetch_all_heads(resolved.retained_git_dir)
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
