"""Project CRUD operations."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

from sqlalchemy import delete, func, insert, select, update

from src.database.tables import (
    archived_tasks,
    chat_analyzer_suggestions,
    events,
    integration_batches,
    project_integration_leases,
    project_constraints,
    projects,
    repos,
    task_comments,
    task_completion_records,
    task_context,
    task_criteria,
    task_dependencies,
    task_integration_checkpoints,
    task_metadata,
    task_results,
    task_subtasks,
    task_tools,
    tasks,
    token_ledger,
    workspaces,
)
from src.models import Project, ProjectConstraint, ProjectStatus
from src.routing.sources import DEFAULT_ROUTER_PLAYBOOK_ID

# Separate from hierarchy/integration writer locks; held before those locks.
REPOSITORY_BINDING_LOCK_NAMESPACE = 0x41515242  # AQRB


class ProjectQueryMixin:
    """Query mixin for project operations.  Expects ``self._engine``."""

    @asynccontextmanager
    async def project_repository_publication(self, project_id: str):
        """Keep a first binding from overlapping a project Git push."""
        async with self._engine.begin() as conn:
            await conn.execute(select(func.pg_advisory_xact_lock_shared(
                REPOSITORY_BINDING_LOCK_NAMESPACE, func.hashtext(project_id),
            )))
            yield

    async def bind_project_repository(
        self, project_id: str, *, repo_url: str, expected_repo_url: str,
        reason: str, operator_id: str,
    ) -> dict:
        """First-binding CAS with publication exclusion and atomic audit.

        CommandHandler owns identity, URL and credential validation. This
        primitive never reassigns an existing authorization or integration.
        """
        if not repo_url or not reason.strip() or not operator_id.strip():
            raise ValueError("repository binding requires a URL, reason and operator identity")

        def refused(code: str, message: str) -> dict:
            return {"success": False, "error_code": code, "error": message}

        async with self._engine.begin() as conn:
            available = (await conn.execute(select(func.pg_try_advisory_xact_lock(
                REPOSITORY_BINDING_LOCK_NAMESPACE, func.hashtext(project_id),
            )))).scalar_one()
            if not available:
                return refused("repository_publication_busy", "A project Git push is in progress")
            await self.lock_hierarchy_project(conn, project_id)
            current = (await conn.execute(select(projects).where(
                projects.c.id == project_id,
            ).with_for_update())).mappings().one_or_none()
            if current is None:
                return refused("project_not_found", "Project not found")
            old_url = current["repo_url"] or ""
            if old_url != expected_repo_url:
                return refused("repository_binding_stale", "The expected repository URL is stale")
            if old_url:
                if old_url == repo_url:
                    return {"success": True, "project_id": project_id,
                            "repo_url": old_url, "changed": False}
                return refused("repository_already_bound",
                               "An existing repository cannot be reassigned")
            configured = (
                current["hierarchical_integration_mode"] != "disabled"
                or current["hierarchical_integration_desired_mode"] != "disabled"
                or current["hierarchical_integration_draining"]
                or current["integration_repository_id"] is not None
            )
            durable = []
            for stmt in (
                select(project_integration_leases.c.project_id).where(
                    project_integration_leases.c.project_id == project_id,
                ),
                select(integration_batches.c.id).where(
                    integration_batches.c.project_id == project_id,
                ),
                select(task_integration_checkpoints.c.task_id).join(
                    tasks, tasks.c.id == task_integration_checkpoints.c.task_id,
                ).where(tasks.c.project_id == project_id),
            ):
                durable.append((await conn.execute(stmt.limit(1))).first() is not None)
            if configured or any(durable):
                return refused(
                    "repository_integration_active",
                    "Repository binding requires disabled integration without publication state",
                )
            expected = (
                projects.c.repo_url.is_(None) if current["repo_url"] is None
                else projects.c.repo_url == expected_repo_url
            )
            written = await conn.execute(update(projects).where(
                projects.c.id == project_id, expected,
            ).values(repo_url=repo_url))
            if written.rowcount != 1:
                return refused("repository_binding_stale", "The expected repository URL is stale")
            payload = {"project_id": project_id, "old_repo_url": old_url,
                       "repo_url": repo_url, "reason": reason, "operator_id": operator_id}
            event_id = await self.log_event(
                "project.repository_bound", project_id=project_id,
                payload=json.dumps(payload, sort_keys=True), conn=conn,
            )
            return {"success": True, "project_id": project_id, "repo_url": repo_url,
                    "changed": True, "event_id": event_id}

    async def create_project(self, project: Project) -> None:
        """Insert a new project row."""
        async with self._engine.begin() as conn:
            await self._validate_hierarchical_integration_project(
                conn,
                project.id,
                project.hierarchical_integration_mode,
                project.integration_repository_id,
            )
            await conn.execute(
                insert(projects).values(
                    id=project.id,
                    name=project.name,
                    credit_weight=project.credit_weight,
                    max_concurrent_agents=project.max_concurrent_agents,
                    status=project.status.value,
                    total_tokens_used=project.total_tokens_used,
                    budget_limit=project.budget_limit,
                    discord_channel_id=project.discord_channel_id,
                    repo_url=project.repo_url,
                    repo_default_branch=project.repo_default_branch,
                    preferred_provider=project.preferred_provider,
                    # Never unbound (routing spec §8): the command layer passes
                    # ``routing.default_router``; a caller that passes nothing
                    # gets that key's default.
                    assignment_playbook_id=(
                        project.assignment_playbook_id or DEFAULT_ROUTER_PLAYBOOK_ID
                    ),
                    integration_mode=project.integration_mode,
                    hierarchical_integration_mode=project.hierarchical_integration_mode,
                    integration_repository_id=project.integration_repository_id,
                    hierarchical_integration_policy=project.hierarchical_integration_policy,
                    hierarchical_integration_desired_mode=(
                        project.hierarchical_integration_desired_mode
                    ),
                    hierarchical_integration_draining=project.hierarchical_integration_draining,
                    hierarchical_integration_generation=(
                        project.hierarchical_integration_generation
                    ),
                    review_delegate_to=project.review_delegate_to,
                    git_identity_name=project.git_identity_name,
                    git_identity_email=project.git_identity_email,
                    created_at=time.time(),
                )
            )

    async def get_project(self, project_id: str, *, conn=None) -> Project | None:
        """Fetch a single project by ID.

        *conn* lets a caller that already owns a transaction read the row on
        it rather than paying another pooled checkout — the claim path's
        three back-to-back pre-reads share one connection this way.
        """
        stmt = select(projects).where(projects.c.id == project_id)
        if conn is not None:
            row = (await conn.execute(stmt)).mappings().fetchone()
            return self._row_to_project(row) if row else None
        async with self._engine.begin() as owned:
            row = (await owned.execute(stmt)).mappings().fetchone()
            return self._row_to_project(row) if row else None

    async def list_projects(
        self,
        status: ProjectStatus | None = None,
    ) -> list[Project]:
        """List all projects, optionally filtered by status."""
        stmt = select(projects)
        if status:
            stmt = stmt.where(projects.c.status == status.value)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return [self._row_to_project(r) for r in result.mappings().fetchall()]

    async def update_project(self, project_id: str, **kwargs) -> None:
        """Update arbitrary project fields."""
        control_fields = {
            "hierarchical_integration_desired_mode",
            "hierarchical_integration_draining",
            "hierarchical_integration_generation",
        }
        if control_fields & kwargs.keys():
            raise ValueError("integration rollout state requires the generation CAS helper")
        values = {}
        for key, value in kwargs.items():
            if isinstance(value, ProjectStatus):
                value = value.value
            values[key] = value
        async with self._engine.begin() as conn:
            if {
                "hierarchical_integration_mode",
                "integration_repository_id",
            } & values.keys():
                current = (
                    (await conn.execute(select(projects).where(projects.c.id == project_id)))
                    .mappings()
                    .one_or_none()
                )
                if current is None:
                    return
                await self._validate_hierarchical_integration_project(
                    conn,
                    project_id,
                    values.get(
                        "hierarchical_integration_mode",
                        current["hierarchical_integration_mode"],
                    ),
                    values.get(
                        "integration_repository_id",
                        current["integration_repository_id"],
                    ),
                )
            await conn.execute(update(projects).where(projects.c.id == project_id).values(**values))

    @staticmethod
    async def _validate_hierarchical_integration_project(
        conn, project_id: str, mode: str, repository_id: str | None
    ) -> None:
        allowed = {"disabled", "observe", "hierarchy", "train", "development"}
        if mode not in allowed:
            raise ValueError(
                "hierarchical_integration_mode must be one of " + ", ".join(sorted(allowed))
            )
        if repository_id is None:
            if mode in {"hierarchy", "train"}:
                raise ValueError(
                    "hierarchy/train mode requires a designated integration repository"
                )
            return
        owner = (
            await conn.execute(select(repos.c.project_id).where(repos.c.id == repository_id))
        ).scalar_one_or_none()
        if owner != project_id:
            raise ValueError("designated integration repository must belong to the same project")

    async def delete_project(self, project_id: str) -> None:
        """Delete a project and all associated data (cascading)."""
        from src.database.queries.hierarchy_queries import HierarchyError
        from src.database.queries.task_references import (
            describe_integration_references,
            find_integration_repository_references,
            find_integration_task_references,
        )

        async with self._engine.begin() as conn:
            # Block concurrent task FK insertion before collecting identities.
            # SQLite needs an actual write to acquire its database write lock.
            await conn.execute(
                select(projects.c.id)
                .where(
                    projects.c.id == project_id,
                )
                .with_for_update()
            )
            # History may resolve to either a live task or its archived
            # snapshot. Deleting the project must not turn that evidence into
            # an orphan just because the task FKs were intentionally removed.
            result = await conn.execute(select(tasks.c.id).where(tasks.c.project_id == project_id))
            task_ids = [r[0] for r in result.fetchall()]
            archived_ids = list(
                (
                    await conn.execute(
                        select(archived_tasks.c.id).where(archived_tasks.c.project_id == project_id)
                    )
                ).scalars()
            )
            found = await find_integration_task_references(conn, [*task_ids, *archived_ids])
            repo_ids = list(
                (await conn.execute(select(repos.c.id).where(repos.c.project_id == project_id))).scalars()
            )
            repository_found = await find_integration_repository_references(conn, repo_ids)
            if found or repository_found:
                references = [*found, *repository_found]
                first = references[0]
                subject = first.get("task_id") or first["repository_id"]
                raise HierarchyError(
                    "integration_history_retained",
                    f"delete is refused: {len(references)} integration audit record(s) name "
                    f"{subject} ({describe_integration_references(references)}) and audit "
                    "history is append-only.",
                    {"references": references},
                )

            for tid in sorted(task_ids):
                await self._assert_pause_cleanup_complete(tid, conn=conn)
                await conn.execute(delete(task_results).where(task_results.c.task_id == tid))
                await conn.execute(
                    delete(task_completion_records).where(task_completion_records.c.task_id == tid)
                )
                await conn.execute(
                    delete(task_dependencies).where(
                        (task_dependencies.c.task_id == tid)
                        | (task_dependencies.c.depends_on_task_id == tid)
                    )
                )
                await conn.execute(delete(task_criteria).where(task_criteria.c.task_id == tid))
                await conn.execute(delete(task_context).where(task_context.c.task_id == tid))
                await conn.execute(delete(task_metadata).where(task_metadata.c.task_id == tid))
                await conn.execute(delete(task_tools).where(task_tools.c.task_id == tid))

            await conn.execute(
                delete(chat_analyzer_suggestions).where(
                    chat_analyzer_suggestions.c.project_id == project_id
                )
            )
            # hooks and hook_runs tables removed (playbooks spec §13 Phase 3)
            await conn.execute(delete(token_ledger).where(token_ledger.c.project_id == project_id))
            # Layout state holds FKs on both ``tasks`` and ``projects``, so
            # it goes before either is deleted; the dirty/job queues are
            # project-scoped bookkeeping that would otherwise be orphaned.
            await self.delete_layout_rows_for_project(project_id, conn=conn)
            await conn.execute(delete(tasks).where(tasks.c.project_id == project_id))
            # Delete after active parents so an in-flight append cannot
            # commit between child cleanup and the parent deletion.
            await conn.execute(
                delete(task_comments).where(task_comments.c.project_id == project_id)
            )
            await conn.execute(
                delete(task_subtasks).where(task_subtasks.c.project_id == project_id)
            )
            await conn.execute(delete(workspaces).where(workspaces.c.project_id == project_id))
            await conn.execute(delete(repos).where(repos.c.project_id == project_id))
            await self.delete_dashboard_documents_for_project(project_id, conn=conn)
            await conn.execute(delete(events).where(events.c.project_id == project_id))
            await conn.execute(
                delete(project_constraints).where(project_constraints.c.project_id == project_id)
            )
            await conn.execute(delete(projects).where(projects.c.id == project_id))

    # ── Project constraint operations ────────────────────────────────

    async def set_project_constraint(self, constraint: ProjectConstraint) -> None:
        """Insert or update the constraint record for a project.

        Uses INSERT-or-REPLACE semantics (SQLite: ON CONFLICT REPLACE,
        PostgreSQL: ON CONFLICT DO UPDATE).  Any fields not provided on
        the new constraint object overwrite the old record — callers must
        merge fields before calling this method if they want additive
        "stacking" behavior.
        """
        async with self._engine.begin() as conn:
            # Delete-then-insert is simpler and works on both SQLite and PG.
            await conn.execute(
                delete(project_constraints).where(
                    project_constraints.c.project_id == constraint.project_id
                )
            )
            await conn.execute(
                insert(project_constraints).values(
                    project_id=constraint.project_id,
                    exclusive=int(constraint.exclusive),
                    max_agents_by_type=json.dumps(constraint.max_agents_by_type),
                    pause_scheduling=int(constraint.pause_scheduling),
                    created_by=constraint.created_by,
                    created_at=constraint.created_at or time.time(),
                )
            )

    async def get_project_constraint(self, project_id: str) -> ProjectConstraint | None:
        """Fetch the active constraint for a project, or None."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                select(project_constraints).where(project_constraints.c.project_id == project_id)
            )
            row = result.mappings().fetchone()
            if not row:
                return None
            return self._row_to_project_constraint(row)

    async def list_project_constraints(self) -> list[ProjectConstraint]:
        """Fetch all active project constraints."""
        async with self._engine.begin() as conn:
            result = await conn.execute(select(project_constraints))
            return [self._row_to_project_constraint(r) for r in result.mappings().fetchall()]

    async def delete_project_constraint(self, project_id: str) -> bool:
        """Remove the constraint for a project.  Returns True if a row was deleted."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                delete(project_constraints).where(project_constraints.c.project_id == project_id)
            )
            return result.rowcount > 0

    @staticmethod
    def _row_to_project_constraint(row) -> ProjectConstraint:
        """Convert a database row to a ProjectConstraint model."""
        mat = row.get("max_agents_by_type", "{}")
        if isinstance(mat, str):
            mat = json.loads(mat)
        return ProjectConstraint(
            project_id=row["project_id"],
            exclusive=bool(row["exclusive"]),
            max_agents_by_type=mat,
            pause_scheduling=bool(row["pause_scheduling"]),
            created_by=row.get("created_by"),
            created_at=row.get("created_at", 0.0),
        )

    @staticmethod
    def _row_to_project(row) -> Project:
        """Convert a database row to a Project model."""
        channel_id = row.get("discord_channel_id")
        if not channel_id:
            channel_id = row.get("discord_control_channel_id")
        return Project(
            id=row["id"],
            name=row["name"],
            credit_weight=row["credit_weight"],
            max_concurrent_agents=row["max_concurrent_agents"],
            status=ProjectStatus(row["status"]),
            total_tokens_used=row["total_tokens_used"],
            budget_limit=row["budget_limit"],
            discord_channel_id=channel_id,
            repo_url=row["repo_url"] if row.get("repo_url") else "",
            repo_default_branch=row["repo_default_branch"]
            if row.get("repo_default_branch")
            else "main",
            preferred_provider=row.get("preferred_provider"),
            # NOT NULL; an empty binding is read as stored, so doctor
            # (``routing.bypassed``) sees the project as unbound.
            assignment_playbook_id=row.get("assignment_playbook_id") or "",
            integration_mode=row.get("integration_mode"),
            hierarchical_integration_mode=(row.get("hierarchical_integration_mode") or "disabled"),
            integration_repository_id=row.get("integration_repository_id"),
            hierarchical_integration_policy=row.get("hierarchical_integration_policy"),
            hierarchical_integration_desired_mode=(
                row.get("hierarchical_integration_desired_mode") or "disabled"
            ),
            hierarchical_integration_draining=bool(
                row.get("hierarchical_integration_draining", False)
            ),
            hierarchical_integration_generation=int(
                row.get("hierarchical_integration_generation", 0)
            ),
            review_delegate_to=row.get("review_delegate_to"),
            git_identity_name=row.get("git_identity_name"),
            git_identity_email=row.get("git_identity_email"),
        )
