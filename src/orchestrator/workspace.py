"""Workspace mixin — workspace preparation, branch/worktree creation, cleanup."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from src.git.manager import GitError, GitManager, is_valid_git_oid
from src.integration.models import BranchKey, Fence
from src.integration.ownership import (
    BranchBusy,
    BranchOwnership,
    BranchOwnershipError,
    StaleFence,
)
from src.models import (
    KIND_MODE_WORKTREE,
    RepoSourceType,
    Task,
    TaskStatus,
    Workspace,
    WorkspaceMode,
)
from src.sessions.provider import SessionHandle

logger = logging.getLogger(__name__)

# git's wording for "that branch is checked out in another worktree".  Both
# phrasings are in the wild depending on which command refused (`switch`,
# `checkout`, `worktree add`).
_BRANCH_BUSY_MARKERS = ("already checked out", "already used by worktree")


def _is_branch_busy_error(exc: Exception) -> bool:
    """True when a git failure is "that branch lives in another worktree"."""
    text = str(exc).lower()
    return any(m in text for m in _BRANCH_BUSY_MARKERS)


@dataclass(frozen=True)
class SlotGrowth:
    """What one lazy slot-pool growth attempt actually did.

    The ensure path used to answer a single question — "is the pool still
    below its cap?" — computed *before* growth ran.  That conflated three
    outcomes a caller must tell apart (the starvation bug this type exists
    to fix):

    * a slot was **created** for this dispatch, so the dispatch should get
      it and an acquisition failure means it lost a race, not that the pool
      is ramping;
    * growth was needed and **stalled** — ``ensure_slots`` raised or created
      nothing — so nothing is coming and the quiet "warming up" wait would
      never clear;
    * the pool is genuinely mid-ramp (``warming``) or already **at cap**
      with everything busy, which are different waits with different
      operator meanings.

    Fields:

    * ``created`` — ``{kind_id: workspace_id}`` for slots created by *this*
      call.  Passed to acquisition as a preference so the dispatch that paid
      for the growth is the one that takes it.
    * ``warming`` — some worktree-mode kind is still below its cap.
    * ``stalled`` — growth was attempted for some kind and produced no row.
    * ``worktree`` — at least one worktree-mode kind with a base was
      considered, i.e. slot semantics apply to this task at all.
    """

    created: dict[str, str] = field(default_factory=dict)
    warming: bool = False
    stalled: bool = False
    worktree: bool = False

    @property
    def grew(self) -> bool:
        """True when this call created at least one slot."""
        return bool(self.created)

    def merged(self, other: SlotGrowth) -> SlotGrowth:
        """Combine two per-kind results into one per-task result."""
        return SlotGrowth(
            created={**self.created, **other.created},
            warming=self.warming or other.warming,
            stalled=self.stalled or other.stalled,
            worktree=self.worktree or other.worktree,
        )


class WorkspaceMixin:
    """Workspace management methods mixed into Orchestrator."""

    def _worktree_slots(self):
        """Lazily-built :class:`WorktreeSlotManager` for this daemon.

        Built on first use rather than in ``__init__`` so installs with
        ``worktrees.enabled: false`` never construct it.
        """
        mgr = getattr(self, "_worktree_slot_manager", None)
        if mgr is None:
            from src.orchestrator.worktree_manager import WorktreeSlotManager

            mgr = WorktreeSlotManager(
                db=self.db,
                git=self.git,
                bus=self.bus,
                config=self.config.worktrees,
                git_mutex=self._git_mutex,
            )
            self._worktree_slot_manager = mgr
        return mgr

    def _worktrees_enabled(self) -> bool:
        """The rollout gate (worktree-execution §5).

        While False every git kind is treated as ``exclusive-clone``
        regardless of its declared ``mode``, so nothing below changes.
        """
        worktrees = getattr(self.config, "worktrees", None)
        return bool(worktrees is not None and worktrees.enabled)

    @staticmethod
    def _remove_sentinel(workspace: str) -> None:
        """Remove the .agent-queue-lock sentinel file from a workspace."""
        sentinel = os.path.join(workspace, ".agent-queue-lock")
        try:
            os.remove(sentinel)
        except OSError:
            pass

    async def _prepare_workspace(self, task: Task, agent) -> str | None:
        async with self._task_control_lock(task.id):
            current = await self.db.get_task(task.id)
            if current and current.status == TaskStatus.PAUSED and current.resume_after is None:
                return None
            return await self._prepare_workspace_locked(task, agent)

    async def _prepare_workspace_locked(self, task: Task, agent) -> str | None:
        """Acquire workspace(s) for the task and prepare the primary one.

        See workspaces-v2 spec §6.5 + §6.6.  This method delegates lock
        acquisition to :func:`acquire_for_task` (which handles multi-kind
        atomically with canonical lock order) and then runs the existing
        git-provisioning sequence on the project-repo attachment.

        Steps:
        1. Under worktree mode, compute the branch-affinity hint (§3.4) that
           steers a retry back to the slot still holding its branch, then
           lazily grow the pool (worktree-execution §3.1) so acquisition has
           something to take.  Growth is deliberately *last*: every await
           between creating a slot and acquiring it is a window in which
           another dispatch takes the row this one paid for, which is how a
           high-priority task ends up repeatedly funding growth it never
           benefits from.
        2. Call ``acquire_for_task`` to obtain a :class:`WorkspaceAttachmentSet`.
           For tasks with no explicit ``requires_kinds``, this synthesizes a
           ``project-repo`` requirement preserving today's behavior.
        3. Stash the AttachmentSet on the orchestrator (Phase 7 reads it).
        4. For a slot workspace, hand off to
           :meth:`_prepare_slot_workspace`; otherwise run the existing
           branch / sentinel / git-provisioning sequence on the project-repo
           attachment's workspace.
        5. Return the workspace path (or ``None`` for tasks with no
           project-repo attachment, e.g. supervisor-style tool-call-only).

        The branch-isolated worktree *fallback* is retired
        (worktree-execution §6.1, §7.4): worktree mode is its principled
        replacement.  Acquisition failure now uniformly returns ``None``, and
        ``_execute_task``'s existing no-workspace PAUSED backoff covers slot
        exhaustion exactly as it covered clone exhaustion.

        Error resilience: git failures (network issues, auth errors) are
        caught and reported via Discord but do NOT prevent the workspace
        from being returned.
        """
        from src.orchestrator.workspace_attachments import (
            AcquisitionFailed,
            acquire_for_task,
        )

        project = await self.db.get_project(task.project_id)
        integration_origin = None
        integration_fence = None
        if getattr(project, "hierarchical_integration_mode", "disabled") in {
            "hierarchy",
            "train",
        }:
            try:
                integration_origin, integration_fence, _integration_role = (
                    await self._hierarchy_origin_and_fence(task, project)
                )
            except (BranchBusy, StaleFence, ValueError) as exc:
                logger.info("Task %s hierarchy workspace wait: %s", task.id, exc)
                self._workspace_wait_reasons[task.id] = "branch_materialization_pending"
                return None
        lock_mode = task.workspace_mode or WorkspaceMode.EXCLUSIVE

        # Directory-isolated mode is stubbed but not yet implemented.
        if lock_mode == WorkspaceMode.DIRECTORY_ISOLATED:
            raise RuntimeError(
                "workspace_mode='directory-isolated' is not yet implemented. "
                "This mode is reserved for future monorepo support where multiple "
                "agents work on the same branch in different directories. "
                "Use 'exclusive' (default) or 'branch-isolated' instead."
            )

        worktrees_enabled = self._worktrees_enabled()

        self._workspace_wait_reasons.pop(task.id, None)
        # ``profile.read_only`` deliberately plays no part in acquisition.
        # It used to skip the lock, which handed read-only agents the kind's
        # base row — the developer's own checkout under worktree mode — and
        # serialized them on its sentinel.  Read-only is a statement about
        # write *intent*, not about isolation, so a read-only task takes an
        # ordinary disposable slot like everything else.  The declarative
        # half of the guarantee (no write tools in the profile's tool list)
        # is unchanged.

        growth = SlotGrowth()
        slot_affinity: dict[str, str] = {}
        if worktrees_enabled:
            # Prefer the slot that already has this task's branch checked
            # out, so a retry cannot collide with its own predecessor slot
            # (§3.4).  Computed *before* growth: a slot that does not exist
            # yet cannot be holding our branch, and every await between
            # growth and acquisition is a window in which a concurrently
            # dispatching task can take the row we just paid for.
            slot_affinity = await self._slot_branch_affinity(task, project)
            # Slots are created lazily, on demand, when acquisition would
            # otherwise find fewer free slots than the cap allows
            # (worktree-execution §3.1 / §6.3).  This is deliberately the
            # last await before ``acquire_for_task``.
            growth = await self._ensure_worktree_slots_for_task(task, project)

        # Branch affinity wins per kind — it is a correctness hint (a slot
        # holding our branch is the only one ``git switch`` will accept),
        # whereas the freshly grown slot is a fairness hint.
        preferred = {**growth.created, **slot_affinity}
        try:
            attachment_set = await acquire_for_task(
                self.db,
                task,
                agent.id,
                worktrees_enabled=worktrees_enabled,
                worktree_slot_cap=(
                    self._project_slot_cap(project) if worktrees_enabled else None
                ),
                preferred_workspaces=preferred or None,
            )
        except AcquisitionFailed:
            reason = self._slot_wait_reason(growth)
            if reason:
                self._workspace_wait_reasons[task.id] = reason
            return None

        # Stash the attachment set so Phase 7 (runtime integration) can
        # consume it without re-acquiring.
        self._task_attachments[task.id] = attachment_set

        primary = attachment_set.first_of_kind("project-repo")
        if primary is None:
            # Tasks with no project-repo attachment (e.g. Supervisor tasks
            # that only need vault) have no primary workspace path — return
            # None so the dispatcher knows there's no cwd to give the agent.
            return None

        ws = primary.workspace
        workspace = ws.workspace_path
        is_git_workspace = primary.kind.is_git_repo

        if worktrees_enabled and ws.is_slot:
            # Worktree mode replaces the whole CLONE/LINK provisioning block
            # and the branch-name computation below (worktree-execution
            # §6.1): reset_slot_for_task owns fetch, reset, clean and branch.
            return await self._prepare_slot_workspace(
                task,
                project,
                primary,
                integration_origin=integration_origin,
                integration_fence=integration_fence,
            )

        is_worktree = ws.source_type == RepoSourceType.WORKTREE

        # Register a git mutex for branch-isolated workspaces.  This ensures
        # that all shared git operations (fetch, gc, pull) routed through
        # GitManager._arun are serialized for this workspace and any
        # worktrees derived from it.  The mutex is keyed by the base
        # workspace path (for worktrees, the parent repo; otherwise the
        # workspace itself).
        if lock_mode == WorkspaceMode.BRANCH_ISOLATED:
            base = (
                await self.git.aworktree_base_path(workspace) if is_worktree else None
            )
            mutex_key = base if base else workspace
            self._git_mutex(mutex_key)  # ensure the lock exists in the dict
            if base:
                self._worktree_base_paths[workspace] = base

        # Layer 2: Filesystem sentinel — detect concurrent access that slipped
        # past the DB-level path lock (e.g. race condition, stale lock).
        # If the sentinel's owner task is no longer IN_PROGRESS, the sentinel
        # is stale (left behind by a crash) and safe to remove.
        #
        # For WORKTREE workspaces, each worktree has its own directory and
        # its own sentinel — no conflict with the base workspace's sentinel.
        sentinel = os.path.join(workspace, ".agent-queue-lock")
        if os.path.exists(sentinel):
            try:
                with open(sentinel) as f:
                    owner_info = f.read().strip()
            except OSError:
                owner_info = "(unreadable)"

            # Check if the sentinel owner is still actively running.
            # Sentinel format: "task_id\nagent_id\n"
            owner_task_id = owner_info.split("\n")[0] if owner_info else ""
            owner_active = False
            if owner_task_id and owner_task_id != task.id:
                owner_task = await self.db.get_task(owner_task_id)
                owner_active = (
                    owner_task is not None and owner_task.status == TaskStatus.IN_PROGRESS
                )
            elif owner_task_id == task.id:
                # Our own sentinel from an earlier attempt whose agent died —
                # a killed session, a daemon stop, a crash.  Treating it as an
                # "active" foreign owner deadlocks the task against itself:
                # by the time this runs the task is already IN_PROGRESS, so the
                # status check says the owner is live, the lock is released,
                # the task pauses for 60s, and the next attempt reads the same
                # sentinel. That loop is unbreakable without deleting the file
                # by hand. Reclaim it instead.
                logger.info(
                    "Workspace %s carries this task's own sentinel — reclaiming",
                    workspace,
                )
                self._remove_sentinel(workspace)

            if owner_active:
                logger.warning(
                    "Workspace %s has active sentinel (owner: %s) — releasing lock",
                    workspace,
                    owner_info,
                )
                await self._release_workspace_and_cleanup(ws)
                return None
            else:
                # Stale sentinel from a crashed/completed task — clean it up
                logger.info(
                    "Workspace %s has stale sentinel (owner: %s) — removing",
                    workspace,
                    owner_info,
                )
                self._remove_sentinel(workspace)
        # Write our sentinel
        try:
            os.makedirs(workspace, exist_ok=True)
            with open(sentinel, "w") as f:
                f.write(f"{task.id}\n{agent.id}\n")
        except OSError as e:
            logger.warning("Failed to write sentinel to %s: %s", workspace, e)

        from src.orchestrator.task_checkpoint import CHECKPOINT_META, restore_checkpoint
        if integration_origin is None and await self.db.get_task_meta(task.id, CHECKPOINT_META):
            try:
                branch = await restore_checkpoint(self.db, self.git, task.id, workspace)
                await self.db.update_task(task.id, branch_name=branch)
                if is_git_workspace:
                    await self._ensure_control_files_excluded(workspace)
                return workspace  # Preserve the resumed task's plan files too.
            except Exception as exc:
                logger.error("Cannot restore paused task %s: %s", task.id, exc)
                self._remove_sentinel(workspace)
                await self._release_workspace_and_cleanup(ws)
                return None

        repo_url = project.repo_url if project else ""
        default_branch = (
            await self._get_default_branch(project, workspace)
            if is_git_workspace
            else (project.repo_default_branch if project else None) or "main"
        )

        # Branch naming strategy:
        # - Root tasks get a fresh branch derived from their ID + title.
        # - Plan subtasks REUSE their parent task's branch name so all
        #   steps in the chain accumulate commits on a single branch.
        #   This is what enables the "one PR for the whole plan" workflow
        #   in _complete_workspace.
        if integration_origin is not None:
            branch_name = task.branch_name
        elif task.is_plan_subtask and task.parent_task_id:
            parent = await self.db.get_task(task.parent_task_id)
            branch_name = (
                parent.branch_name
                if parent and parent.branch_name
                else GitManager.make_branch_name(task.id, task.title)
            )
        else:
            branch_name = GitManager.make_branch_name(task.id, task.title)

        # Git handoff is fail-closed. A workspace without verified managed
        # excludes or usable branch state is released instead of launched.
        try:
            if integration_origin is not None and integration_fence is not None:
                branch_name = await self._prepare_exact_origin_workspace(
                    task,
                    project,
                    primary,
                    integration_origin,
                    integration_fence,
                )
            elif is_worktree:
                # Legacy WORKTREE row: a pre-existing branch-isolated worktree
                # from before worktree-execution retired that fallback.  New
                # ones are never created; these are drained as their tasks
                # finish.  The base is resolved from git itself now, not from
                # the retired ``.worktrees-<base>/`` filename convention.
                # Fetch is automatically serialized by the GitManager lock
                # provider — no need for explicit mutex acquisition here.
                if not is_git_workspace or not await self.git.avalidate_checkout(workspace):
                    raise GitError(f"legacy worktree is not a valid Git checkout: {workspace}")
                await self._ensure_control_files_excluded(workspace)
                base_path = await self.git.aworktree_base_path(workspace)
                if base_path and await self.git.ahas_remote(base_path):
                    await self.git._arun(["fetch", "origin"], cwd=base_path)
            elif is_git_workspace:
                # Workspace source types determine the git setup strategy:
                #
                # CLONE: The orchestrator manages the full clone lifecycle.
                #   - First use: clone from repo_url into the workspace path.
                #   - Subsequent uses: fetch + ensure clean default branch.
                #
                # LINK: The workspace points to a pre-existing local checkout
                #   (e.g. the developer's own repo).  The orchestrator only
                #   validates the directory exists.
                #
                # In both cases the orchestrator no longer creates task branches.
                # The agent receives branch-name + default-branch in its prompt
                # and handles branch creation, merge, and push itself.
                if ws.source_type == RepoSourceType.CLONE:
                    if not await self.git.avalidate_checkout(workspace):
                        os.makedirs(os.path.dirname(workspace), exist_ok=True)
                        if repo_url:
                            await self.git.acreate_checkout(repo_url, workspace)
                        # Re-detect default branch now that the repo is cloned.
                        # The initial detection (above) may have fallen back to
                        # "main" because the workspace didn't exist yet.
                        default_branch = await self._get_default_branch(project, workspace)

                elif ws.source_type == RepoSourceType.LINK:
                    if not os.path.isdir(workspace):
                        raise GitError(f"linked workspace path does not exist: {workspace}")

                # Ensure workspace is on a clean, up-to-date default branch.
                # The agent will create/switch to the task branch per its prompt.
                if await self.git.avalidate_checkout(workspace):
                    # The daemon's own control files (``.aq/claim.json``, the
                    # worktree sentinel, ``.agent-queue-lock``) live untracked
                    # inside this checkout.  Only Git's resolved
                    # ``info/exclude`` keeps
                    # them out of ``git status`` — and therefore out of the
                    # verify phase's ``git add -A`` auto-commit — and until
                    # now only worktree-slot provisioning wrote that block.
                    await self._ensure_control_files_excluded(workspace)
                    # Discard any uncommitted changes left by a previous task
                    # so the checkout to default_branch doesn't fail due to
                    # conflicts.  This is especially important for LINK
                    # workspaces without remotes, where no hard-reset follows.
                    #
                    # We first abort any in-progress operations (merge/rebase)
                    # and remove stale lock files, then force-clean the workspace.
                    # The old approach (checkout -- . + clean -fd) failed when
                    # the workspace was in a mid-merge/rebase state or had
                    # staged-but-uncommitted changes.
                    if await self.git.ahas_uncommitted_changes(workspace):
                        try:
                            await self.git.aforce_clean_workspace(workspace)
                        except GitError:
                            pass  # Best-effort cleanup
                    # Fetch is automatically serialized by the GitManager
                    # lock provider for branch-isolated workspaces.
                    if await self.git.ahas_remote(workspace):
                        await self.git._arun(["fetch", "origin"], cwd=workspace)
                    try:
                        await self.git._arun(["checkout", default_branch], cwd=workspace)
                    except GitError:
                        pass  # May already be on default branch
                    if await self.git.ahas_remote(workspace):
                        await self.git._arun(
                            ["reset", "--hard", f"origin/{default_branch}"],
                            cwd=workspace,
                        )
                else:
                    raise GitError(f"workspace is not a valid Git checkout: {workspace}")

            # Update task branch in DB
            await self.db.update_task(task.id, branch_name=branch_name)
        except Exception as e:
            # Layer 3: Git failure means no launch — release workspace and
            # clean up the sentinel so another task can use this workspace.
            logger.error("Git setup failed for task %s in %s: %s", task.id, workspace, e)
            await self._emit_text_notify(
                f"**Git Error:** Task `{task.id}` — branch setup failed: {e}\n"
                f"Workspace released. Task will retry when a workspace is available.",
                project_id=task.project_id,
            )
            self._remove_sentinel(workspace)
            await self._release_workspace_and_cleanup(ws)
            return None

        # Clean up ALL plan files from previous tasks to prevent:
        # 1. A new task from discovering a stale plan that belongs to another task
        # 2. A new task failing to write its own plan because the file already exists
        # This covers both archived plans (.claude/plans/) and primary plan files
        # (.claude/plan.md, plan.md, etc.).
        if integration_origin is None:
            await self._cleanup_plan_files_before_task(
                workspace, task.id, branch_name=branch_name, default_branch=default_branch
            )

        return workspace

    async def _hierarchy_origin_and_fence(
        self, task: Task, project
    ) -> tuple[dict, Fence, str]:
        """Resolve exact origin/target and the server-derived current role."""
        repository_id = getattr(project, "integration_repository_id", None)
        if not repository_id or task.repo_id != repository_id:
            raise ValueError("task is not bound to the designated repository")
        repair = await self.db.get_active_integration_repair_for_task(task.id)
        if repair is not None:
            if repair.get("writer_kind") != "repair_delegate":
                raise ValueError("integration repair writer kind is not a delegate")
            if repair["target_kind"] == "parent":
                subject_id = repair["parent_task_id"]
                subject = await self.db.get_task(subject_id)
                if subject is None or subject.repo_id != repository_id:
                    raise ValueError("repair parent target does not exist")
                branch = subject.branch_name or ""
                origin = await self.db.get_task_branch_origin_for_promotion(
                    subject_id, repository_id
                )
                if origin is None:
                    raise ValueError("repair parent has no persisted branch origin")
                origin = dict(origin) | {"base_sha": repair["stage_starting_sha"]}
            elif repair["target_kind"] == "batch":
                batch = await self.db.get_integration_batch(repair["batch_id"])
                if (
                    batch is None
                    or batch["project_id"] != task.project_id
                    or batch["repository_id"] != repository_id
                    or not batch["integration_branch"]
                ):
                    raise ValueError("repair batch target does not exist")
                branch = batch["integration_branch"]
                origin = {
                    "base_sha": repair["stage_starting_sha"],
                    "reserved": True,
                    "materialized": True,
                    "operation_id": repair["id"],
                }
            else:
                raise ValueError("repair operation target kind is invalid")
            if task.branch_name != branch:
                raise ValueError("repair delegate branch does not match its target")
            target = BranchKey(repository_id=repository_id, branch=branch)
            owner = await BranchOwnership(self.db).get_owner(target)
            if (
                owner is None
                or owner["owner_id"] != task.id
                or owner["owner_role"] != "repair"
                or owner["handoff_state"] != "reserved"
            ):
                raise BranchBusy("repair branch is not reserved by this delegate")
            return (
                origin,
                Fence(
                    target=target,
                    owner_id=task.id,
                    token=int(owner["fence_token"]),
                ),
                "repair",
            )
        subject_id = task.id
        operation = await self.db.get_active_integration_verifier_for_task(task.id)
        if operation is not None:
            subject_id = operation["parent_task_id"]
        subject = await self.db.get_task(subject_id)
        if subject is None:
            raise ValueError("integration owner subject does not exist")
        origin = await self.db.get_task_branch_origin_for_promotion(subject_id, repository_id)
        if origin is None or not origin.get("reserved") or not origin.get("materialized"):
            raise ValueError("exact branch origin is not materialized")
        branch = subject.branch_name or ""
        if branch != f"aq/{subject_id}" or task.branch_name != branch:
            raise ValueError("task branch does not match its canonical origin")
        target = BranchKey(repository_id=repository_id, branch=branch)
        ownership = BranchOwnership(self.db)
        owner = await ownership.get_owner(target)
        role = str(owner["owner_role"]) if owner is not None else ""
        expected_role = "verifier" if operation is not None or subject_id == task.id and role == "verifier" else "worker"
        if (
            owner is None
            or owner["owner_id"] != task.id
            or role != expected_role
            or owner["handoff_state"] != "reserved"
        ):
            raise BranchBusy("canonical branch is not reserved by this task")
        if role == "verifier":
            checkpoint = await self.db.get_integration_checkpoint(subject_id)
            if checkpoint is None or not checkpoint.get("episode_id"):
                raise ValueError("verifier target has no active parent episode")
            origin = dict(origin) | {"base_sha": checkpoint["checkpoint_sha"]}
        return (
            origin,
            Fence(target=target, owner_id=task.id, token=int(owner["fence_token"])),
            role,
        )

    async def _prepare_exact_origin_workspace(
        self,
        task: Task,
        project,
        attachment,
        origin: dict,
        fence: Fence,
    ) -> str:
        """Prepare any enabled checkout at its pinned origin under one owner fence."""
        ws = attachment.workspace
        workspace = ws.workspace_path
        branch = fence.target.branch
        base_sha = str(origin["base_sha"])
        repair = await self.db.get_active_integration_repair_for_task(task.id)
        if repair is not None and repair.get("retained_workspace_id"):
            provenance = repair.get("retained_handoff") or {}
            if (
                repair["retained_workspace_id"] != ws.id
                or task.preferred_workspace_id != ws.id
                or ws.locked_by_task_id != task.id
                or provenance.get("new_task_id") != task.id
                or provenance.get("new_fence_token") != fence.token
                or provenance.get("workspace_id") != ws.id
            ):
                raise BranchBusy("retained repair workspace binding is stale")
            current_branch = await self.git.aget_current_branch(workspace, strict=True)
            current_head = (
                await self.git._arun(["rev-parse", "HEAD"], cwd=workspace)
            ).strip().lower()
            if (
                current_branch != branch
                or current_head != provenance.get("head_sha")
            ):
                raise BranchBusy("retained repair workspace contents changed")
            return branch
        owner = await BranchOwnership(self.db).get_owner(fence.target)
        role = owner["owner_role"] if owner else None
        async with BranchOwnership(self.db).mutation_exclusion(
            fence, expected_role=role
        ):
            if ws.is_slot:
                return await self._worktree_slots().reset_slot_for_task(
                    ws,
                    task,
                    base_branch=base_sha,
                    resume_branch=None,
                    kind=attachment.kind,
                )

            if ws.source_type == RepoSourceType.CLONE:
                if not await self.git.avalidate_checkout(workspace):
                    os.makedirs(os.path.dirname(workspace), exist_ok=True)
                    if project.repo_url:
                        await self.git.acreate_checkout(project.repo_url, workspace)
            elif ws.source_type == RepoSourceType.LINK:
                if not os.path.isdir(workspace):
                    raise GitError(f"linked workspace path does not exist: {workspace}")
            else:
                raise GitError("enabled exact-origin preparation requires a clone or link")

            if not await self.git.avalidate_checkout(workspace):
                raise GitError(f"workspace is not a valid Git checkout: {workspace}")
            await self._ensure_control_files_excluded(workspace)
            if await self.git.ahas_uncommitted_changes(workspace):
                await self.git.aforce_clean_workspace(workspace)
            if await self.git.ahas_remote(workspace):
                await self.git._arun(["fetch", "origin"], cwd=workspace)
            await self.git._arun(["checkout", "-B", branch, base_sha], cwd=workspace)
            actual_head = await self.git._arun(["rev-parse", "HEAD"], cwd=workspace)
            if actual_head != base_sha:
                raise GitError(
                    f"exact origin checkout resolved {actual_head or 'no HEAD'}, expected {base_sha}"
                )
        return branch

    async def _ensure_control_files_excluded(self, workspace: str) -> bool:
        """Write and verify the managed block at Git's exact exclude path.

        Worktree slots get this from :class:`WorktreeSlotManager` at
        provisioning; an exclusive clone or linked checkout (``worktrees.enabled:
        false``) has to get it here, or ``.aq/claim.json`` and friends end up in
        ``auto-commit: uncommitted changes from task <id>`` and — in direct
        mode — on the project's default branch.  Resolution uses
        ``git rev-parse --path-format=absolute --git-path info/exclude`` with
        the older-Git fallback in :class:`GitManager`, so separate Git dirs
        and linked worktrees are covered.  Failure is fatal to handoff: an
        agent or pool session must never receive an unprotected checkout.
        """
        from src.orchestrator.worktree_manager import (
            WorktreeSlotManager,
            resolve_managed_exclude_path,
        )

        exclude_path = await resolve_managed_exclude_path(self.git, workspace)
        return WorktreeSlotManager.ensure_git_exclude_path(exclude_path)

    @staticmethod
    def _project_slot_cap(project) -> int:
        """Slot cap for a project — design §2.2: slot count == agent cap.

        One definition, used by slot growth, acquisition and capacity
        counting alike, so the three cannot drift apart.
        """
        if project is None:
            return 1
        return max(1, getattr(project, "max_concurrent_agents", 1) or 1)

    @staticmethod
    def _slot_wait_reason(growth: SlotGrowth) -> str | None:
        """The wait reason for an acquisition failure under worktree mode.

        Four distinct waits, all of which used to collapse into the single
        quiet ``slot_warming`` pause (or into the loud, and under worktree
        mode simply wrong, "use /add-workspace" notice):

        * ``slot_lost_race`` — a slot *was* created for this dispatch and a
          concurrently dispatching task took it first.  Nothing is ramping
          and nothing is broken; the task just needs to be back in the READY
          pool fast, so priority — not backoff order — decides the next
          winner.
        * ``slot_stalled`` — growth was needed and produced nothing.  The
          "warming up" wait would never clear; this has to be visible.
        * ``slot_warming`` — the pool is genuinely mid-ramp and some other
          dispatch is provisioning.
        * ``slots_full`` — the pool is at cap with every slot busy.  Honest
          contention, and not something ``/add-workspace`` can fix.

        ``None`` means "not a slot wait" — the legacy clone path, whose
        behavior is deliberately unchanged.
        """
        if growth.grew:
            return "slot_lost_race"
        if growth.stalled:
            return "slot_stalled"
        if growth.warming:
            return "slot_warming"
        if growth.worktree:
            return "slots_full"
        return None

    async def _ensure_worktree_slots(self, project, kind_id: str) -> SlotGrowth:
        """Lazily grow the slot pool for one worktree-mode kind.

        Single-kind body of :meth:`_ensure_worktree_slots_for_task`'s loop,
        factored out so a non-task caller — the pool launch path
        (``PoolsMixin._launch_pool_session``), which has no ``Task`` to
        synthesize requirements from — can warm slots for exactly the kind
        it needs.  See that method's docstring for the growth policy (design
        §3.1: one slot per dispatch, never fatal).

        Returns a :class:`SlotGrowth` describing what happened, so the caller
        can (a) prefer the slot it just paid for and (b) tell a healthy ramp
        apart from a stalled one and from a genuinely full pool.  It used to
        return a single ``warming`` bool computed *before* growth ran, which
        made all three indistinguishable — see :class:`SlotGrowth`.
        """
        cap = self._project_slot_cap(project)

        kind = await self.db.resolve_workspace_kind(project.id, kind_id)
        if kind is None or not kind.is_git_repo or kind.mode != KIND_MODE_WORKTREE:
            return SlotGrowth()

        base = await self.db.find_worktree_base(project.id, kind.id)
        if base is None:
            # No clone to hang worktrees off yet.  Provisioning the base
            # itself stays the operator's job (spec §7.3).
            logger.debug(
                "No base workspace for worktree-mode kind %s in project %s",
                kind.id,
                project.id,
            )
            return SlotGrowth()

        slots = await self.db.list_slots_for_base(base.id)
        self._register_slot_bases(slots, base.workspace_path)

        in_cap = [s for s in slots if (s.slot_index or 0) < cap]
        free = [s for s in in_cap if s.locked_by_agent_id is None]
        warming = len(in_cap) < cap
        if free or len(in_cap) >= cap:
            # Something is acquirable, or we are already at cap: no growth.
            return SlotGrowth(warming=warming, worktree=True)

        known = {s.id for s in slots}
        try:
            grown = await self._worktree_slots().ensure_slots(
                project, base, kind, min(cap, len(in_cap) + 1)
            )
            self._register_slot_bases(grown, base.workspace_path)
        except Exception as e:
            logger.warning(
                "Could not provision a worktree slot for kind %s in %s: %s",
                kind.id,
                project.id,
                e,
            )
            return SlotGrowth(warming=warming, stalled=True, worktree=True)

        fresh = [s for s in grown if s.id not in known and (s.slot_index or 0) < cap]
        if not fresh:
            # ``ensure_slots`` swallows per-slot git failures and returns what
            # exists (see its docstring).  Nothing new means nothing is coming:
            # the "warming up" wait would never clear on its own, so say so
            # rather than pausing quietly forever.
            logger.warning(
                "Worktree slot growth produced no new slot for kind %s in %s "
                "(%d/%d slots, none free) — the pool is not ramping",
                kind.id,
                project.id,
                len(in_cap),
                cap,
            )
            return SlotGrowth(warming=warming, stalled=True, worktree=True)

        return SlotGrowth(created={kind.id: fresh[0].id}, warming=warming, worktree=True)

    async def _ensure_worktree_slots_for_task(self, task: Task, project) -> SlotGrowth:
        """Lazily grow the slot pool for every worktree-mode kind the task needs.

        Design §3.1: slots are created on demand, up to the project's agent
        cap.  Growth is **one slot per dispatch** when nothing is free — a
        cap-4 project reaching four slots takes four dispatches, which is the
        same ramp the agents themselves take, and avoids paying four
        ``worktree add`` plus four ``worktree_setup`` runs in one tick.

        Failures here are never fatal: acquisition simply finds nothing free
        and the task takes the existing no-workspace PAUSED backoff.

        Returns the merged :class:`SlotGrowth` across the task's kinds.  The
        caller feeds ``created`` back into acquisition as a preference so the
        dispatch that paid for a slot is the one that takes it, and uses the
        remaining flags to pick a wait reason.
        """
        from src.orchestrator.workspace_attachments import effective_requirements

        growth = SlotGrowth()
        seen: set[str] = set()
        for req in await effective_requirements(self.db, task):
            if req.kind_id in seen:
                continue
            seen.add(req.kind_id)
            growth = growth.merged(await self._ensure_worktree_slots(project, req.kind_id))
        return growth

    async def _resume_branch_for(self, task: Task) -> str | None:
        """The branch a task *resumes* rather than creates, if any.

        Plan subtasks accumulate onto their parent's branch so the whole plan
        lands as one PR — that maps to the continuation/resume path rather
        than a fresh ``aq/<task_id>`` (worktree-execution §6.1).
        """
        if not (task.is_plan_subtask and task.parent_task_id):
            return None
        parent = await self.db.get_task(task.parent_task_id)
        if parent is None:
            return None
        return parent.branch_name or None

    async def _slot_branch_affinity(self, task: Task, project) -> dict[str, str]:
        """``{kind_id: workspace_id}`` for slots already holding this task's branch.

        Design §3.4 leaves a released slot **on its last task's branch**, so a
        retry that the scheduler hands a *different* slot collides with its
        own predecessor: ``git switch aq/<task_id>`` is refused, the task
        pauses for the no-workspace backoff, retries, and collides again —
        indefinitely, because nothing moves the old slot off the branch.
        Preferring the slot that already holds the branch removes the
        collision at its source instead of reporting it.

        The preference is a *hint*, computed fresh from ``git worktree list``
        per acquisition (see
        :meth:`WorktreeSlotManager.find_slot_holding_branch`) — nothing is
        recorded on the task, so nothing can go stale.  When the holder is
        busy, acquisition falls back to any free slot: a hard wait would park
        the task behind an unrelated long-running one, and the branch-held
        reporting still covers the collision that follows.

        For a plan subtask the branch in question is the *parent's* shared
        branch (§4.4).  A running sibling holds its slot locked, so the hint
        cannot be taken and the existing sibling wait is unchanged; when no
        sibling is running the next subtask lands back on the slot that last
        advanced the plan branch, which is strictly better than colliding.
        """
        from src.orchestrator.workspace_attachments import effective_requirements
        from src.orchestrator.worktree_manager import task_branch_name

        branch = await self._resume_branch_for(task) or task_branch_name(task.id)
        cap = self._project_slot_cap(project)
        affinity: dict[str, str] = {}
        seen: set[str] = set()
        for req in await effective_requirements(self.db, task):
            if req.kind_id in seen or req.preferred_workspace_id:
                continue
            seen.add(req.kind_id)
            try:
                kind = await self.db.resolve_workspace_kind(project.id, req.kind_id)
                if kind is None or not kind.is_git_repo or kind.mode != KIND_MODE_WORKTREE:
                    continue
                base = await self.db.find_worktree_base(project.id, kind.id)
                if base is None:
                    continue
                slots = [
                    s
                    for s in await self.db.list_slots_for_base(base.id)
                    if (s.slot_index or 0) < cap
                ]
                holder = await self._worktree_slots().find_slot_holding_branch(
                    base, slots, branch
                )
            except Exception as e:  # a hint is never worth failing dispatch over
                logger.debug(
                    "Task %s: branch affinity lookup failed for kind %s: %s",
                    task.id,
                    req.kind_id,
                    e,
                )
                continue
            if holder:
                affinity[req.kind_id] = holder
        return affinity

    def _register_slot_bases(self, slots, base_path: str) -> None:
        """Record ``slot_path -> base_path`` for the sync git-lock resolver.

        Replaces the retired ``.worktrees-<base>/`` path parsing
        (worktree-execution §6.2): the mapping is now data, read from slot
        rows, not a filename convention.
        """
        for slot in slots:
            self._worktree_base_paths[slot.workspace_path] = base_path
        self._git_mutex(base_path)  # ensure the lock exists for the resolver

    def _recover_worktree_base_paths(self, all_workspaces: list[Workspace]) -> None:
        """Rebuild ``slot_path -> base_path`` for *every* slot row at startup.

        ``_resolve_git_lock`` must be sync (``GitManager._arun`` calls it
        inline), so it reads a dict rather than asking git.  Building that
        dict only in ``_prepare_workspace`` made it an incremental cache with
        a restart-shaped hole: a task that was IN_PROGRESS when the daemon
        died never re-enters prepare, so its slot stayed unregistered and its
        ``fetch`` stopped serializing against the base's shared object store.

        Populating it from one ``list_workspaces()`` sweep during recovery
        makes the map a *projection of the DB* instead — every slot that
        exists is registered before any git command can run.
        """
        by_id = {ws.id: ws for ws in all_workspaces}
        for ws in all_workspaces:
            if not ws.is_slot or not ws.base_workspace_id:
                continue
            base = by_id.get(ws.base_workspace_id)
            if base is None:
                logger.warning(
                    "Slot %s references missing base workspace %s — git "
                    "serialization for it cannot be restored",
                    ws.id,
                    ws.base_workspace_id,
                )
                continue
            self._register_slot_bases([ws], base.workspace_path)

    async def _prepare_slot_workspace(
        self,
        task: Task,
        project,
        attachment,
        *,
        integration_origin: dict | None = None,
        integration_fence: Fence | None = None,
    ) -> str | None:
        """Prepare an acquired slot worktree for *task*.  Returns its path.

        Worktree-execution §3.2.  Everything the legacy path did with
        sentinels, branch naming and clone/link provisioning is owned by
        ``reset_slot_for_task`` instead:

        * no ``.agent-queue-lock`` file — the DB lock plus the slot's own
          ``.aq-worktree.json`` are the record (design §2.5);
        * no plan-file cleanup — the reset already produced a pristine tree,
          and the legacy cleanup ends by checking out the default branch,
          which would take the slot straight back off its task branch (and
          fails anyway when the base has that branch checked out).
        """
        ws = attachment.workspace
        slot_dir = ws.workspace_path

        base = (
            await self.db.get_workspace(ws.base_workspace_id)
            if ws.base_workspace_id
            else None
        )
        if base is not None:
            self._register_slot_bases([ws], base.workspace_path)

        resume_branch = None if integration_origin is not None else await self._resume_branch_for(task)

        try:
            reset_kwargs = {
                "base_branch": integration_origin["base_sha"]
                if integration_origin is not None
                else None,
                "resume_branch": resume_branch,
                "kind": attachment.kind,
            }
            if integration_fence is not None and integration_origin is not None:
                branch_name = await self._prepare_exact_origin_workspace(
                    task, project, attachment, integration_origin, integration_fence
                )
            else:
                # Keep the legacy call signature stable for existing slot
                # managers and tests while the rollout flag is disabled.
                reset_kwargs.pop("base_branch")
                branch_name = await self._worktree_slots().reset_slot_for_task(
                    ws, task, **reset_kwargs
                )
        except Exception as e:
            if _is_branch_busy_error(e) and resume_branch:
                # Two plan subtasks of the same parent resume the *same*
                # branch, and git refuses to check one branch out in two
                # worktrees.  That is a scheduling wait, not a git error:
                # siblings serialize onto the plan branch by design
                # (worktree-execution §4.4), and the loser retries after the
                # normal PAUSED backoff.  Reporting it as a Git Error once per
                # attempt is pure noise.
                #
                # Only a *resume* can have a sibling: without ``resume_branch``
                # the task owns ``aq/<task_id>`` alone, so a branch-busy error
                # there is a stuck slot, not scheduling.  Calling that a
                # sibling wait logged "waits for branch None" and silently
                # looped forever.
                logger.info(
                    "Task %s waits for branch %s — a sibling holds it in another "
                    "slot; retrying after the no-workspace backoff",
                    task.id,
                    resume_branch,
                )
                self._workspace_wait_reasons[task.id] = "branch_busy"
            elif _is_branch_busy_error(e):
                from src.orchestrator.worktree_manager import task_branch_name

                # The task's own branch is checked out in another slot.  A
                # released slot deliberately stays on its last task's branch
                # (worktree-execution §3.4), so a retry that lands on a
                # *different* slot collides with its own predecessor — and a
                # slot left on a deleted task's branch pins it for good.
                # Nothing frees this on its own: say so instead of retrying
                # in silence.  ``aq doctor --check worktrees.orphans`` names
                # the slot still holding it.
                logger.warning(
                    "Task %s cannot take slot %s: branch %s is checked out in "
                    "another worktree (%s)",
                    task.id,
                    slot_dir,
                    task_branch_name(task.id),
                    e,
                )
                self._workspace_wait_reasons[task.id] = "branch_held"
            else:
                logger.error(
                    "Worktree slot reset failed for task %s in %s: %s",
                    task.id,
                    slot_dir,
                    e,
                )
                await self._emit_text_notify(
                    f"**Git Error:** Task `{task.id}` — worktree slot setup failed: {e}\n"
                    f"Slot released. Task will retry when a slot is available.",
                    project_id=task.project_id,
                )
            await self._release_workspace_and_cleanup(ws)
            return None

        await self.db.update_task(task.id, branch_name=branch_name)
        return slot_dir

    async def _release_workspace_and_cleanup(self, ws: Workspace) -> None:
        """Release a workspace lock, cleaning up *legacy* worktrees only.

        * **Slot rows** are unlocked and nothing else (worktree-execution
          §6.1).  A slot outlives every task that uses it — the branch, not
          the worktree, is the durable artifact — and only
          ``WorktreeSlotManager.reap_slot`` (phase 4) ever deletes one.
        * **Legacy branch-isolated worktree rows** keep the old behavior:
          remove the git worktree and delete the dynamically created record.
        * Everything else just releases the DB lock.
        """
        if ws.is_slot:
            await self.db.release_workspace(ws.id)
            return
        if ws.source_type == RepoSourceType.WORKTREE:
            await self._cleanup_worktree_workspace(ws)
        else:
            await self.db.release_workspace(ws.id)

    async def aconfirm_integration_owner_handoff(self, owner: dict) -> bool:
        """Stop and detach an attached integration writer before fencing it out.

        A database unlock is not termination evidence: slot worktrees retain
        their branch after ordinary release.  This path uses the provider's
        uncached ``confirm_stopped`` probe, then requires a slot to be truly
        detached before releasing it.  If any proof is unavailable, retain
        the lock and return ``False`` so the ownership record stays fenced.
        """
        session_id = owner.get("session_id")
        workspace_id = owner.get("workspace_id")
        from src.orchestrator.workspace_attachments import (
            integration_handoff_release_is_confirmed,
            mark_integration_handoff_released,
        )

        if await integration_handoff_release_is_confirmed(self.db, owner):
            return True
        if not session_id or not workspace_id:
            return False
        session = await self.db.get_session(session_id)
        workspace = await self.db.get_workspace(workspace_id)
        repository = await self.db.get_repo(str(owner.get("repository_id") or ""))
        task = await self.db.get_task(session.task_id) if session and session.task_id else None
        if (
            session is None
            or workspace is None
            or repository is None
            or task is None
            or workspace.locked_by_task_id != session.task_id
            or workspace.project_id != repository.project_id
            or session.project_id != repository.project_id
            or task.project_id != repository.project_id
            or task.repo_id != repository.id
            or task.branch_name != owner.get("ref")
            or os.path.realpath(session.work_dir) != os.path.realpath(workspace.workspace_path)
        ):
            return False
        current_branch = await self.git.aget_current_branch(
            workspace.workspace_path, strict=True
        )
        if current_branch not in {owner.get("ref"), "HEAD"}:
            return False
        try:
            provider = self.session_providers.create(session.provider, self.config)
            handle = SessionHandle(
                name=session.name,
                provider=session.provider,
                instance_token=session.instance_token,
            )
            await provider.stop(handle, grace=2.0)
            if not await provider.confirm_stopped(handle):
                return False
        except Exception:
            logger.warning("Could not confirm integration writer %s stopped", session_id, exc_info=True)
            return False

        try:
            from src.orchestrator.workspace_attachments import (
                detach_slot_for_integration_handoff,
                detach_workspace_for_integration_handoff,
            )

            if workspace.is_slot:
                detached = await detach_slot_for_integration_handoff(
                    self.db,
                    self.git,
                    self._git_mutex,
                    workspace,
                    expected_branch=str(owner["ref"]),
                )
            else:
                detached = await detach_workspace_for_integration_handoff(
                    self.git,
                    self._git_mutex,
                    workspace,
                    expected_branch=str(owner["ref"]),
                )
            if not detached:
                return False
        except Exception:
            logger.warning(
                "Could not detach integration workspace %s",
                workspace.id,
                exc_info=True,
            )
            return False

        current_workspace = await self.db.get_workspace(workspace.id)
        if (
            current_workspace is None
            or current_workspace.locked_by_task_id != session.task_id
        ):
            return False
        if session.state != "stopped" or session.desired_state != "stopped":
            await self.db.update_session(
                session.id,
                state="stopped",
                desired_state="stopped",
                end_reason="integration_handoff",
            )
        if not await mark_integration_handoff_released(
            self.db, owner, workspace=workspace, task_id=session.task_id
        ):
            return False
        released = await self.db.get_workspace(workspace.id)
        return released is not None and released.locked_by_task_id is None

    async def aconfirm_integration_owner_stopped_for_repair(
        self, owner: dict
    ) -> dict | None:
        """Stop one exact writer without touching its retained repair checkout."""
        session_id = owner.get("session_id")
        workspace_id = owner.get("workspace_id")
        if not session_id or not workspace_id or owner.get("handoff_state") != "attached":
            return None
        session = await self.db.get_session(session_id)
        workspace = await self.db.get_workspace(workspace_id)
        repository = await self.db.get_repo(str(owner.get("repository_id") or ""))
        task = await self.db.get_task(session.task_id) if session and session.task_id else None
        if (
            session is None
            or workspace is None
            or repository is None
            or task is None
            or workspace.locked_by_task_id != task.id
            or workspace.project_id != repository.project_id
            or session.project_id != repository.project_id
            or session.task_id != task.id
            or task.repo_id != repository.id
            or task.branch_name != owner.get("ref")
            or owner.get("owner_id") != task.id
            or os.path.realpath(session.work_dir)
            != os.path.realpath(workspace.workspace_path)
        ):
            return None
        if not await self.db.update_session_instance(
            session.id,
            session.instance_token,
            desired_state="stopped",
        ):
            return None
        try:
            provider = self.session_providers.create(session.provider, self.config)
            handle = SessionHandle(
                name=session.name,
                provider=session.provider,
                instance_token=session.instance_token,
            )
            await provider.stop(handle, grace=2.0)
            if not await provider.confirm_stopped(handle):
                return None
            current_branch = await self.git.aget_current_branch(
                workspace.workspace_path, strict=True
            )
            head_sha = (
                await self.git._arun(["rev-parse", "HEAD"], cwd=workspace.workspace_path)
            ).strip().lower()
        except Exception:
            logger.warning(
                "Could not prove retained repair writer %s stopped",
                session_id,
                exc_info=True,
            )
            return None
        if current_branch != owner.get("ref") or not is_valid_git_oid(head_sha):
            return None
        if not await self.db.update_session_instance(
            session.id,
            session.instance_token,
            require_desired_state="stopped",
            state="stopped",
            end_reason="integration_repair_retained_handoff",
        ):
            return None
        return {
            "session_id": session.id,
            "workspace_id": workspace.id,
            "head_sha": head_sha,
            "instance_token": session.instance_token,
        }

    async def arelease_integration_writer_for_retry(self, task, *, reason: str) -> bool | None:
        """Prove an enabled writer stopped, then restore its task reservation.

        ``None`` means the project is unmanaged and legacy cleanup applies.
        ``False`` means termination/detach is unknown, so the workspace lock
        must remain held.  ``True`` means the exact attachment was atomically
        released and the same task now owns a fresh reserved fence for retry.
        """
        project = await self.db.get_project(task.project_id)
        if getattr(project, "hierarchical_integration_mode", "disabled") not in {
            "hierarchy",
            "train",
        }:
            return None
        repository_id = getattr(project, "integration_repository_id", None)
        if not repository_id or task.repo_id != repository_id or not task.branch_name:
            return False
        target = BranchKey(repository_id=repository_id, branch=task.branch_name)
        ownership = BranchOwnership(
            self.db, confirm_handoff=self.aconfirm_integration_owner_handoff
        )
        owner = await ownership.get_owner(target)
        if owner is None or owner["owner_id"] != task.id or owner["owner_role"] != "worker":
            return False
        if owner["handoff_state"] == "reserved":
            return True
        fence = Fence(target=target, owner_id=task.id, token=int(owner["fence_token"]))
        try:
            await ownership.transfer(fence, task.id, "worker")
        except BranchOwnershipError:
            logger.warning(
                "Task %s retains its integration workspace after %s: stop/detach unconfirmed",
                task.id,
                reason,
            )
            return False
        return True

    async def arelease_never_attached_integration_launch(
        self, task, *, agent_id: str, workspace_id: str
    ) -> bool:
        """Release exact task resources after ownership won before attachment."""
        project = await self.db.get_project(task.project_id)
        repository_id = getattr(project, "integration_repository_id", None)
        if (
            getattr(project, "hierarchical_integration_mode", "disabled")
            not in {"hierarchy", "train"}
            or not repository_id
            or task.repo_id != repository_id
            or not task.branch_name
        ):
            return False
        from src.orchestrator.workspace_attachments import (
            release_never_attached_integration_launch,
        )

        return await release_never_attached_integration_launch(
            self.db,
            repository_id=repository_id,
            branch=task.branch_name,
            task_id=task.id,
            agent_id=agent_id,
            workspace_id=workspace_id,
        )

    async def _cleanup_worktree_workspace(self, ws: Workspace) -> None:
        """Remove a *legacy* git worktree and delete its workspace record.

        Intended for pre-worktree-execution branch-isolated rows, retired
        with them in phase 6.  **Slot rows short-circuit here, not only at
        the call sites** — a slot outlives every task that uses it (design
        §3.4) and only ``WorktreeSlotManager.reap_slot`` (phase 4) may
        delete one.

        The guard lives in this method deliberately, because three callers
        reach it: :meth:`_release_workspace_and_cleanup`,
        :meth:`_release_workspaces_for_task` and
        ``Orchestrator._recover_stale_state`` — and the last walks *every*
        ``source_type=WORKTREE`` row, which now includes slots.  Since
        ``aworktree_base_path`` resolves a slot successfully (unlike the
        retired ``_get_worktree_base_path`` filename parsing), an unguarded
        call would run ``git worktree remove`` with a ``--force`` fallback
        against a live slot on every daemon restart, destroying uncommitted
        work and every warm cache.  One guard here covers all three callers.
        """
        if ws.is_slot:
            logger.debug(
                "Not deleting slot workspace %s at %s — slots are reaped, never "
                "cleaned up (worktree-execution §3.4/§6)",
                ws.id,
                ws.workspace_path,
            )
            await self.db.release_workspace(ws.id)
            return
        base_path = self._worktree_base_paths.get(
            ws.workspace_path
        ) or await self.git.aworktree_base_path(ws.workspace_path)
        if base_path:
            try:
                # Serialize worktree removal via the git mutex to prevent
                # concurrent modifications to the shared .git/worktrees/ dir.
                async with self._git_mutex(base_path):
                    await self.git.aremove_worktree(base_path, ws.workspace_path)
            except GitError as e:
                logger.warning("Failed to remove worktree %s: %s", ws.workspace_path, e)
                # Best-effort: try to remove the directory directly
                import shutil

                try:
                    shutil.rmtree(ws.workspace_path, ignore_errors=True)
                except Exception:
                    pass
        await self.db.release_workspace(ws.id)
        await self.db.delete_workspace(ws.id)
        logger.info("Cleaned up worktree workspace %s at %s", ws.id, ws.workspace_path)

    async def _release_workspaces_for_task(self, task_id: str) -> None:
        async with self._task_control_lock(task_id):
            current = await self.db.get_task(task_id)
            if current and current.status == TaskStatus.PAUSED and current.resume_after is None:
                return
            await self._release_workspaces_for_task_locked(task_id)

    async def _release_workspaces_for_task_locked(self, task_id: str) -> None:
        """Release all workspace locks for a task, cleaning up worktrees.

        Wraps ``db.release_workspaces_for_task()`` with worktree awareness.
        For tasks that used *legacy* branch-isolated worktrees, this removes
        the git worktree and deletes the dynamically created workspace record
        before releasing locks.  Slot worktrees and regular workspaces are
        released normally — never deleted.
        Also discards any cached :class:`WorkspaceAttachmentSet` for the task.
        """
        # Find all workspaces locked by this task
        all_ws = await self.db.list_workspaces()
        worktree_ws = [
            ws
            for ws in all_ws
            if ws.locked_by_task_id == task_id
            and ws.source_type == RepoSourceType.WORKTREE
            # Slots survive their tasks (worktree-execution §3.4); they are
            # released by the bulk update below like any other row.
            and not ws.is_slot
        ]

        # Clean up worktree workspaces first (remove git worktree + delete record)
        for ws in worktree_ws:
            self._remove_sentinel(ws.workspace_path)
            await self._cleanup_worktree_workspace(ws)

        # Release any remaining (non-worktree) workspaces via bulk operation
        await self.db.release_workspaces_for_task(task_id)

        # Drop the cached attachment set — the next prepare will rebuild it.
        self._task_attachments.pop(task_id, None)

    async def _cleanup_plan_files_before_task(
        self,
        workspace: str,
        task_id: str,
        *,
        branch_name: str | None = None,
        default_branch: str = "main",
    ) -> None:
        """Remove all plan files from previous tasks before starting a new one.

        Cleans up:

        1. **Primary plan files** (``.claude/plan.md``, ``plan.md``, etc.) on
           the current (default) branch — leftover files can cause agents to
           fail to write their own plan ("file already exists") or lead to
           stale plan re-discovery.
        2. **Archived plan files** (``.claude/plans/<task_id>-plan.md``) —
           clearly attributable to specific tasks via their filename prefix.
           Any that don't belong to the current task are removed.
        3. **Plan files on the task branch** (if ``branch_name`` is provided
           and the branch already exists locally or as a remote tracking
           branch).  When a task is retried, or a plan subtask follows a
           sibling, the agent may have committed plan files directly to the
           task branch via ``git add && git commit``.  Those files would
           reappear when the new agent checks out the branch.

        .. note::

           Deletions are committed using direct ``git add -A`` + ``git commit``
           instead of :meth:`GitManager.acommit_all`, because ``acommit_all``
           excludes plan files from commits (via ``_PLAN_FILE_EXCLUDES``).
        """
        import glob as _glob

        plan_patterns = self.config.auto_task.plan_file_patterns

        def _remove_plan_files() -> bool:
            """Delete primary plan files + archived plans.  Returns True if any deleted."""
            deleted = False
            for pattern in plan_patterns:
                full_pattern = os.path.join(workspace, pattern)
                for fpath in _glob.glob(full_pattern):
                    if os.path.isfile(fpath):
                        try:
                            os.remove(fpath)
                            deleted = True
                            logger.info(
                                "Pre-task cleanup: removed plan file %s (task %s)",
                                fpath,
                                task_id,
                            )
                        except OSError as e:
                            logger.warning(
                                "Pre-task cleanup: failed to remove plan file %s: %s",
                                fpath,
                                e,
                            )

            plans_dir = os.path.join(workspace, ".claude", "plans")
            if os.path.isdir(plans_dir):
                try:
                    for entry in os.listdir(plans_dir):
                        if entry.startswith(task_id):
                            continue
                        fpath = os.path.join(plans_dir, entry)
                        if os.path.isfile(fpath) and entry.endswith(".md"):
                            os.remove(fpath)
                            deleted = True
                            logger.info(
                                "Pre-task cleanup: removed archived plan %s (task %s)",
                                fpath,
                                task_id,
                            )
                except OSError as e:
                    logger.warning(
                        "Pre-task cleanup: failed to clean plans dir %s: %s",
                        plans_dir,
                        e,
                    )
            return deleted

        async def _commit_plan_deletions(msg: str) -> None:
            """Commit all pending changes including plan file deletions.

            Uses direct ``git add -A`` + ``git commit`` instead of
            ``acommit_all`` which excludes plan files via
            ``_PLAN_FILE_EXCLUDES``.
            """
            try:
                if not await self.git.avalidate_checkout(workspace):
                    return
                await self.git._arun(["add", "-A"], cwd=workspace)
                result = await self.git._arun_subprocess(
                    ["git", "diff", "--cached", "--quiet"],
                    cwd=workspace,
                    timeout=self.git._GIT_TIMEOUT,
                )
                if result.returncode != 0:
                    await self.git._arun(["commit", "-m", msg], cwd=workspace)
            except Exception as e:
                logger.warning("Pre-task cleanup: failed to commit: %s", e)

        # ── 1. Clean current (default) branch ─────────────────────────────
        if _remove_plan_files():
            await _commit_plan_deletions(
                f"chore: clean up stale plan files before task {task_id}",
            )

        # ── 2. Clean the task branch if it already exists ─────────────────
        # When a task is retried (or a plan subtask follows a sibling),
        # the task branch may carry plan files committed by a previous
        # agent run.  Checkout that branch, purge plan files, commit the
        # deletion, and return to the default branch so the agent starts
        # from a clean state.
        if branch_name and await self.git.avalidate_checkout(workspace):
            switched = False
            try:
                await self.git._arun(["checkout", branch_name], cwd=workspace)
                switched = True
            except GitError:
                pass  # Branch does not exist — nothing to clean

            if switched:
                try:
                    if _remove_plan_files():
                        await _commit_plan_deletions(
                            f"chore: clean up stale plan files before task {task_id}",
                        )
                finally:
                    # Always return to the default branch even if cleanup failed.
                    try:
                        await self.git._arun(["checkout", default_branch], cwd=workspace)
                    except Exception as e:
                        logger.error(
                            "Pre-task cleanup: CRITICAL — failed to return to %s "
                            "after cleaning task branch %s: %s",
                            default_branch,
                            branch_name,
                            e,
                        )

    async def _slot_workspace_at(self, path: str) -> Workspace | None:
        """The slot ``workspaces`` row whose directory is *path*, if any.

        Several cleanup paths only ever receive a filesystem path (they run
        off ``pipeline_ctx.workspace_path``), but slot and clone need opposite
        treatment, so the row has to be recovered from the path.  Comparison
        is normalized — case-folded on Windows — because paths reach here from
        both the DB and git.
        """
        if not path:
            return None
        target = os.path.normcase(os.path.abspath(path))
        try:
            rows = await self.db.list_workspaces()
        except Exception:  # pragma: no cover - defensive
            return None
        for ws in rows:
            if not ws.is_slot or not ws.workspace_path:
                continue
            if os.path.normcase(os.path.abspath(ws.workspace_path)) == target:
                return ws
        return None

    async def _cleanup_workspace_for_next_task(
        self,
        workspace: str | None,
        default_branch: str,
        task_id: str,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Ensure the workspace is clean and on the default branch.

        Called when a task reaches a terminal non-success state (BLOCKED,
        FAILED with retries exhausted) or when a task is reopened for
        verification retry, to prevent dirty workspace state from blocking
        or confusing the next task assigned to this workspace.

        Cleanup strategy for a **clone** (best-effort, most-conservative-first):
        0. Abort any in-progress git operations and remove stale lock files.
        1. Commit any uncommitted changes (preserves work).
        2. Stash if commit fails (preserves work, less accessible).
        3. Force-clean workspace as last resort (reset --hard + clean -fdx).
        4. Switch to the default branch.

        **A slot worktree takes a different route entirely** — see
        :meth:`WorktreeSlotManager.restore_slot_after_task`.  Every rung of
        the ladder above is wrong inside a worktree: the stash stack is shared
        with the base and every sibling slot (design §3.2 forbids touching
        it), ``clean -fdx`` destroys the warm caches that justify reusing a
        slot, and ``checkout <default_branch>`` fails outright because the
        base already has that branch checked out.
        """
        if not workspace:
            return

        slot_ws = await self._slot_workspace_at(workspace)
        if slot_ws is not None:
            try:
                await self._worktree_slots().restore_slot_after_task(
                    slot_ws, task_id=task_id
                )
            except Exception as e:  # never block the terminal transition
                logger.warning(
                    "Task %s: could not restore slot %s after task: %s",
                    task_id,
                    workspace,
                    e,
                )
            return

        try:
            if not await self.git.avalidate_checkout(workspace):
                return
        except Exception:
            return

        # Step 0: Abort any in-progress operations and remove lock files
        # so that subsequent git commands don't fail due to stale state.
        try:
            await self.git.aabort_in_progress_operations(workspace)
        except Exception as e:
            logger.warning(
                "Task %s: abort in-progress operations failed during cleanup: %s",
                task_id,
                e,
            )

        # Step 1: Handle uncommitted changes
        try:
            has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
            if has_uncommitted:
                # Try to commit first (safest — preserves work).
                # Use --no-verify to bypass pre-commit hooks which can
                # reject the commit (e.g. ruff formatting) and prevent
                # workspace cleanup.
                try:
                    await self.git.acommit_all(
                        workspace,
                        f"auto-commit: workspace cleanup after task {task_id}",
                        exclude_plans=False,
                        no_verify=True,
                        event_bus=self.bus,
                        project_id=project_id,
                        agent_id=agent_id,
                    )
                    logger.info(
                        "Task %s: auto-committed changes during workspace cleanup",
                        task_id,
                    )
                except Exception:
                    # Commit failed — try stash (still preserves work)
                    try:
                        await self.git._arun(
                            [
                                "stash",
                                "--include-untracked",
                                "-m",
                                f"auto-stash: workspace cleanup for task {task_id}",
                            ],
                            cwd=workspace,
                        )
                        logger.info(
                            "Task %s: stashed changes during workspace cleanup",
                            task_id,
                        )
                    except Exception:
                        # Last resort — force-clean (reset --hard + clean -fdx)
                        try:
                            clean = await self.git.aforce_clean_workspace(workspace)
                            if clean:
                                logger.info(
                                    "Task %s: force-cleaned workspace during cleanup",
                                    task_id,
                                )
                            else:
                                logger.warning(
                                    "Task %s: force-clean did not fully clean workspace",
                                    task_id,
                                )
                        except Exception as force_err:
                            logger.warning(
                                "Task %s: all cleanup attempts failed: %s",
                                task_id,
                                force_err,
                            )
                            return
        except Exception as e:
            logger.warning(
                "Task %s: failed to check uncommitted changes during cleanup: %s",
                task_id,
                e,
            )

        # Step 2: Switch to default branch
        try:
            current = await self.git.aget_current_branch(workspace)
            if current != default_branch:
                await self.git._arun(["checkout", default_branch], cwd=workspace)
                logger.info(
                    "Task %s: switched to '%s' during workspace cleanup",
                    task_id,
                    default_branch,
                )
        except Exception as e:
            logger.warning(
                "Task %s: failed to switch to '%s' during cleanup: %s",
                task_id,
                default_branch,
                e,
            )
