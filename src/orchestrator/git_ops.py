"""Git operations mixin — merge, push, PR creation, verification."""

from __future__ import annotations

import logging
import os

from src.git.manager import GitError
from src.notifications.builder import build_task_detail
from src.notifications.events import (
    MergeConflictEvent,
    PushFailedEvent,
)
from src.models import (
    PhaseResult,
    PipelineContext,
    RepoConfig,
    Task,
    TaskStatus,
)
from src.orchestrator.merge_slot import (
    acquire_merge_slot,
    release_merge_slot,
    renew_merge_slot,
)

logger = logging.getLogger(__name__)


class GitOpsMixin:
    """Git operations methods mixed into Orchestrator."""

    async def _is_last_subtask(self, task: Task) -> bool:
        """Check if this subtask is the final one to complete in a plan chain.

        Returns True when every sibling subtask (all tasks sharing the same
        ``parent_task_id``) has already reached COMPLETED status.  This
        determines whether the post-completion workflow should trigger the
        "final step" actions: merge to default branch or create a PR.

        Intermediate subtasks only commit to the shared branch — they do
        not merge or create PRs, keeping the chain flowing without human
        intervention until the last step.
        """
        if not task.parent_task_id:
            return True
        siblings = await self.db.get_subtasks(task.parent_task_id)
        for sibling in siblings:
            if sibling.id == task.id:
                continue
            if sibling.status != TaskStatus.COMPLETED:
                return False
        return True

    async def _merge_and_push(
        self,
        task: Task,
        repo: RepoConfig,
        workspace: str,
        *,
        _max_retries: int = 3,
    ) -> None:
        """Merge the task branch into default and push.

        .. deprecated::
            No longer called by the completion pipeline.  The agent now
            handles merging and pushing via its prompt instructions.  Kept
            for manual recovery use cases.
        """
        has_remote = await self.git.ahas_remote(workspace)

        if has_remote:
            # sync_and_merge handles fetch, hard-reset, merge, and push
            # with retry.  max_retries counts *retries* after the first
            # attempt, so subtract 1 from _max_retries (total attempts).
            success, error = await self.git.async_and_merge(
                workspace,
                task.branch_name,
                repo.default_branch,
                max_retries=max(_max_retries - 1, 0),
            )
            if not success:
                if error == "merge_conflict":
                    await self._emit_notify(
                        "notify.merge_conflict",
                        MergeConflictEvent(
                            task=build_task_detail(task),
                            branch=task.branch_name or "",
                            target_branch=repo.default_branch,
                            project_id=task.project_id,
                        ),
                    )
                else:
                    # error starts with "push_failed: …"
                    await self._emit_notify(
                        "notify.push_failed",
                        PushFailedEvent(
                            task=build_task_detail(task),
                            branch=repo.default_branch,
                            error_detail=(
                                f"Could not push after {_max_retries} attempts. "
                                f"Workspace may be diverged. Details: {error}"
                            ),
                            project_id=task.project_id,
                        ),
                    )
                # Recovery: reset workspace to origin state so it's clean
                # for the next task.  After a failed push the local default
                # branch may contain un-pushed merge commits; hard-resetting
                # to origin discards them.
                try:
                    await self.git.arecover_workspace(workspace, repo.default_branch)
                except Exception:
                    pass  # best-effort recovery
                return
        else:
            # LINK / INIT repos have no remote — just merge locally.
            merged = await self.git.amerge_branch(
                workspace,
                task.branch_name,
                repo.default_branch,
            )
            if not merged:
                # Rebase fallback: rebase the task branch onto the default
                # branch and retry the merge.  This resolves conflicts caused
                # by the task branch being based on a stale snapshot.
                rebased = await self.git.arebase_onto(
                    workspace,
                    task.branch_name,
                    repo.default_branch,
                )
                if rebased:
                    merged = await self.git.amerge_branch(
                        workspace,
                        task.branch_name,
                        repo.default_branch,
                    )
            if not merged:
                await self._emit_notify(
                    "notify.merge_conflict",
                    MergeConflictEvent(
                        task=build_task_detail(task),
                        branch=task.branch_name or "",
                        target_branch=repo.default_branch,
                        project_id=task.project_id,
                    ),
                )
                # Recovery: ensure we're on the default branch so the
                # workspace is clean for the next task.  merge_branch()
                # already aborts the merge, but we make sure we're on the
                # right branch as a safety net.
                try:
                    await self.git._arun(
                        ["checkout", repo.default_branch],
                        cwd=workspace,
                    )
                except Exception:
                    pass  # best-effort recovery
                return

            # Clean up the task branch after successful local merge
            try:
                await self.git.adelete_branch(
                    workspace,
                    task.branch_name,
                    delete_remote=False,
                )
            except Exception:
                pass  # branch cleanup is best-effort
            return

        # Clean up the task branch after successful merge + push
        try:
            await self.git.adelete_branch(
                workspace,
                task.branch_name,
                delete_remote=has_remote,
            )
        except Exception:
            pass  # branch cleanup is best-effort

    async def _create_pr_for_task(
        self,
        task: Task,
        repo: RepoConfig,
        workspace: str,
    ) -> str | None:
        """Push the task branch and create a PR. Returns the PR URL or None.

        .. deprecated::
            No longer called by the completion pipeline.  The agent now
            creates PRs via its prompt instructions.  Kept for manual use.

        Uses ``force_with_lease=True`` when pushing the task branch so that
        retries (e.g. after a failed PR creation where the push succeeded)
        don't fail with a non-fast-forward error.  ``--force-with-lease`` is
        safe here because the task branch is owned exclusively by this agent —
        no other user is expected to push to it (resolves **G5**).
        """
        if not await self.git.ahas_remote(workspace):
            # No remote — notify user to review the branch locally
            await self._emit_text_notify(
                f"**Approval Required:** Task `{task.id}` — {task.title}\n"
                f"Branch `{task.branch_name}` is ready for review in `{workspace}`.\n"
                f"Use the `approve_task` command to complete it.",
                project_id=task.project_id,
            )
            return None

        try:
            # Use --force-with-lease so the push succeeds even when the
            # branch was previously pushed (e.g. task retries or subtask
            # chains that push intermediate results).  Task branches are
            # owned by a single agent, so force-pushing is safe.
            await self.git.apush_branch(
                workspace,
                task.branch_name,
                force_with_lease=True,
                event_bus=self.bus,
                project_id=task.project_id,
            )
        except Exception as e:
            await self._emit_notify(
                "notify.push_failed",
                PushFailedEvent(
                    task=build_task_detail(task),
                    branch=task.branch_name or "",
                    error_detail=str(e),
                    project_id=task.project_id,
                ),
            )
            return None

        try:
            pr_url = await self.git.acreate_pr(
                workspace,
                branch=task.branch_name,
                title=task.title,
                body=f"Automated PR for task `{task.id}`.\n\n{task.description[:500]}",
                base=repo.default_branch,
                event_bus=self.bus,
                project_id=task.project_id,
            )
            return pr_url
        except Exception as e:
            await self._emit_text_notify(
                f"**PR Creation Failed:** Task `{task.id}` — {e}\n"
                f"Branch `{task.branch_name}` has been pushed. Create a PR manually.",
                project_id=task.project_id,
            )
            return None

    async def _task_has_code_changes(
        self, workspace: str, min_files: int = 3, min_lines: int = 50
    ) -> bool:
        """Check if the current branch has substantial non-plan code changes.

        Uses ``git diff --stat`` against the merge-base with the default branch,
        excluding plan file paths.  Returns True if the diff exceeds the given
        thresholds (files changed OR lines changed), indicating the plan was
        likely already implemented during this task.

        Args:
            workspace: Path to the git checkout.
            min_files: Minimum number of changed files to consider "substantial".
            min_lines: Minimum number of lines changed (insertions + deletions).

        Returns:
            True if the branch has substantial code changes beyond plan files.
        """
        try:
            if not await self.git.avalidate_checkout(workspace):
                return False

            default_branch = await self.git.aget_default_branch(workspace)

            # Find the merge-base between HEAD and the default branch
            try:
                merge_base = await self.git._arun(
                    ["merge-base", f"origin/{default_branch}", "HEAD"],
                    cwd=workspace,
                )
            except GitError:
                # No remote tracking or no common ancestor — can't compare
                try:
                    merge_base = await self.git._arun(
                        ["merge-base", default_branch, "HEAD"],
                        cwd=workspace,
                    )
                except GitError:
                    return False

            # Get diff stat excluding plan files and non-code artifacts
            stat_output = await self.git._arun(
                [
                    "diff",
                    "--stat",
                    f"{merge_base}..HEAD",
                    "--",
                    ".",
                    # Exclude plan files
                    ":!.claude/plan.md",
                    ":!plan.md",
                    ":!.claude/plans/",
                    ":!docs/plans/",
                    ":!docs/plan.md",
                    ":!plans/",
                    # Exclude non-code artifacts (notes, logs, test results)
                    ":!notes/",
                    ":!*.log",
                    ":!test-results*",
                ],
                cwd=workspace,
            )

            if not stat_output:
                return False

            # Parse the summary line, e.g.:
            #  "10 files changed, 200 insertions(+), 50 deletions(-)"
            lines = stat_output.strip().splitlines()
            summary = lines[-1] if lines else ""

            import re

            files_match = re.search(r"(\d+)\s+files?\s+changed", summary)
            insertions_match = re.search(r"(\d+)\s+insertions?", summary)
            deletions_match = re.search(r"(\d+)\s+deletions?", summary)

            files_changed = int(files_match.group(1)) if files_match else 0
            insertions = int(insertions_match.group(1)) if insertions_match else 0
            deletions = int(deletions_match.group(1)) if deletions_match else 0
            total_lines = insertions + deletions

            return files_changed >= min_files or total_lines >= min_lines

        except Exception as e:
            logger.debug("_task_has_code_changes failed (will proceed normally): %s", e)
            return False

    async def _discover_and_store_plan(self, task: Task, workspace: str) -> bool:
        """Discover a plan file, parse it, and store the parsed data for approval.

        Called after a task completes successfully.  Searches the workspace
        for a plan file (e.g. ``.claude/plan.md``) using configurable glob
        patterns, parses it, and stores the parsed plan data as task_context
        entries so the plan can be presented to the user for approval.

        The plan file is archived to ``.claude/plans/<task_id>-plan.md``
        after processing to prevent re-processing if the workspace is reused.

        Returns True if a plan was found, parsed, and stored for approval.
        """
        from src.plan_parser import find_plan_file, read_plan_file

        config = self.config.auto_task
        if not config.enabled:
            return False

        # Prevent recursive plan explosion: subtasks must not generate
        # further sub-plans.
        if task.is_plan_subtask:
            return False

        # Git-diff heuristic: if the task already made substantial code
        # changes (beyond the plan file itself), the plan was likely
        # already executed during this task — skip generating subtasks.
        if config.skip_if_implemented:
            try:
                project = await self.db.get_project(task.project_id) if task.project_id else None
                default_branch = await self._get_default_branch(project, workspace)
                if await self.git.ahas_non_plan_changes(workspace, default_branch):
                    logger.info(
                        "Auto-task: skipping plan approval for task %s — "
                        "branch has substantial code changes beyond the plan file, "
                        "indicating the plan was already implemented",
                        task.id,
                    )
                    return False
            except Exception as e:
                # On any error, fall through to normal behaviour
                logger.debug(
                    "Auto-task: skip_if_implemented check failed for task %s: %s",
                    task.id,
                    e,
                )

        plan_path = find_plan_file(workspace, config.plan_file_patterns)
        if not plan_path:
            logger.debug(
                "Auto-task: no plan file found for task %s in workspace %s (searched patterns: %s)",
                task.id,
                workspace,
                config.plan_file_patterns,
            )
            return False

        # Staleness check: compare the plan file's mtime against the
        # recorded execution start time.  If the plan file predates the
        # agent's execution start, it was written by a previous task and
        # should not be attributed to this one.  This prevents stale plan
        # files (left behind by failed cleanups) from being incorrectly
        # picked up by unrelated tasks sharing the same workspace.
        exec_start = self._task_exec_start.get(task.id)
        if exec_start is not None:
            try:
                plan_mtime = os.path.getmtime(plan_path)
                # Use a 2-second tolerance to account for filesystem
                # timestamp granularity (some filesystems round to 1s).
                # In practice, stale plans are minutes/hours old.
                if plan_mtime < exec_start - 2.0:
                    logger.warning(
                        "Auto-task: ignoring stale plan file %s for task %s "
                        "(plan mtime %.0f < exec start %.0f — file "
                        "predates this task's execution)",
                        plan_path,
                        task.id,
                        plan_mtime,
                        exec_start,
                    )
                    # Archive it so it's not rediscovered by future tasks
                    try:
                        plans_dir = os.path.join(workspace, ".claude", "plans")
                        os.makedirs(plans_dir, exist_ok=True)
                        stale_archive = os.path.join(plans_dir, f"stale-{task.id}-plan.md")
                        os.rename(plan_path, stale_archive)
                    except OSError:
                        pass
                    return False
            except OSError as e:
                logger.debug(
                    "Auto-task: staleness check failed for %s: %s (proceeding)",
                    plan_path,
                    e,
                )

        try:
            raw = read_plan_file(plan_path)
        except Exception as e:
            logger.warning("Auto-task: failed to read plan file %s: %s", plan_path, e)
            return False

        if not raw or not raw.strip():
            logger.info("Auto-task: plan file %s is empty", plan_path)
            return False

        logger.info("Auto-task: found plan file %s for task %s", plan_path, task.id)

        # Archive the plan file for traceability (so it won't be re-processed
        # if the workspace is reused for another task).
        archived_path = None
        try:
            plans_dir = os.path.join(workspace, ".claude", "plans")
            os.makedirs(plans_dir, exist_ok=True)
            archived_path = os.path.join(plans_dir, f"{task.id}-plan.md")
            os.rename(plan_path, archived_path)
        except OSError:
            pass

        # Store the raw plan content as task_context.  The actual
        # plan-to-task splitting is done by the supervisor LLM at
        # approval time (not by algorithmic parsing here).
        await self.db.add_task_context(
            task.id,
            type="plan_raw",
            label="Plan Raw Content",
            content=raw,
        )
        if archived_path:
            await self.db.add_task_context(
                task.id,
                type="plan_archived_path",
                label="Plan Archived Path",
                content=archived_path,
            )

        logger.info(
            "Auto-task: stored plan for task %s — awaiting user approval",
            task.id,
        )
        return True

    # ── Completion pipeline ────────────────────────────────────────────────
    #
    # The completion pipeline runs: commit → plan_generate → merge.
    # Plan generation runs BEFORE merge so the plan file is archived
    # (and the archival committed) before the branch is merged to the
    # default branch.  This prevents the plan file from persisting on
    # main and being re-discovered by subsequent tasks.
    # Each phase receives a PipelineContext and returns a PhaseResult.

    async def _run_completion_pipeline(self, ctx: PipelineContext) -> tuple[str | None, bool]:
        """Run the post-completion pipeline. Returns (pr_url, completed_ok).

        Phase execution strategy:
        - **plan_discover**: Non-critical — if it crashes, log and continue
          to the verify phase.  Plan discovery failure should not prevent
          git verification and auto-remediation from running.
        - **verify**: Critical — if it crashes or returns STOP, the task
          cannot be marked completed.
        """
        # Phase 1: Plan discovery (non-critical)
        try:
            result = await self._phase_plan_discover(ctx)
            if result == PhaseResult.STOP or result == PhaseResult.ERROR:
                logger.warning(
                    "Pipeline phase 'plan_discover' returned %s for task %s — continuing to verify",
                    result,
                    ctx.task.id,
                )
        except Exception as e:
            logger.error(
                "Pipeline phase 'plan_discover' failed for task %s: %s — continuing to verify",
                ctx.task.id,
                e,
                exc_info=True,
            )

        # Phase 2: Git verification (critical)
        try:
            result = await self._phase_verify(ctx)
        except Exception as e:
            logger.error(
                "Pipeline phase 'verify' failed for task %s: %s",
                ctx.task.id,
                e,
                exc_info=True,
            )
            return (ctx.pr_url, False)
        if result == PhaseResult.STOP:
            return (ctx.pr_url, False)
        if result == PhaseResult.ERROR:
            return (ctx.pr_url, False)

        # Phase 3: Integration (worktree-mode only).  For exclusive-clone
        # tasks the verify phase already handled the merge; for worktree
        # slots this is where rebase + push + merge happens under the
        # per-project merge slot lease.  Worktree-execution spec §6.5.
        if await self._task_is_worktree_mode(ctx):
            try:
                result = await self._phase_integrate(ctx)
            except Exception as e:
                logger.error(
                    "Pipeline phase 'integrate' failed for task %s: %s",
                    ctx.task.id,
                    e,
                    exc_info=True,
                )
                return (ctx.pr_url, False)
            if result in (PhaseResult.STOP, PhaseResult.ERROR):
                return (ctx.pr_url, False)

        return (ctx.pr_url, True)

    async def _phase_verify(self, ctx: PipelineContext) -> PhaseResult:
        """Pipeline phase: verify the agent left the workspace in the expected git state.

        Replaces the old _phase_commit + _phase_merge.  The agent is now
        responsible for committing, merging, and pushing via its prompt
        instructions.  This phase only *checks* the result and reopens the
        task with specific feedback when something is off.

        Verification scenarios:

        * **Intermediate subtask** — expect: on task branch, no uncommitted.
        * **Final task / final subtask, requires_approval** — expect: on task
          branch, branch pushed, PR exists.
        * **Final task / final subtask, no approval** — expect: on default
          branch, no uncommitted, in sync with origin.
        * **No-change task** — on default branch with no diff → pass.
        """
        workspace = ctx.workspace_path
        task = ctx.task

        # Skip verification if the agent exited with an error — bad git state
        # is a symptom, not the root cause.  Let normal error handling deal
        # with the task instead of reopening for git fixes.
        if ctx.output.exit_code and ctx.output.exit_code != 0:
            logger.info(
                "Task %s: agent exited with non-zero exit code (%d), skipping git verification",
                task.id,
                ctx.output.exit_code,
            )
            # Still auto-remediate uncommitted changes so the workspace is
            # clean for the next task.  Without this, a crashed agent leaves
            # dirty state that bleeds into subsequent tasks.
            if workspace and await self.git.avalidate_checkout(workspace):
                try:
                    if await self.git.ahas_uncommitted_changes(workspace):
                        current = await self.git.aget_current_branch(workspace)
                        await self._auto_remediate_uncommitted(
                            workspace,
                            task.id,
                            current,
                            project_id=task.project_id,
                            agent_id=ctx.agent.id,
                        )
                except Exception as e:
                    logger.warning(
                        "Task %s: auto-remediation during skip failed: %s",
                        task.id,
                        e,
                    )
            return PhaseResult.CONTINUE

        # Skip verification if the task opted out (e.g. research/investigation tasks)
        if task.skip_verification:
            logger.info("Task %s: skip_verification=True, skipping git verification", task.id)
            return PhaseResult.CONTINUE

        if not workspace or not await self.git.avalidate_checkout(workspace):
            return PhaseResult.CONTINUE
        if not task.branch_name:
            return PhaseResult.CONTINUE

        default_branch = ctx.default_branch
        has_remote = await self.git.ahas_remote(workspace)
        current_branch = await self.git.aget_current_branch(workspace)
        has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)

        # Determine which scenario we're in
        is_intermediate = task.is_plan_subtask and not await self._is_last_subtask(task)
        if task.is_plan_subtask and task.parent_task_id:
            parent = await self.db.get_task(task.parent_task_id)
            requires_approval = parent.requires_approval if parent else task.requires_approval
        else:
            requires_approval = task.requires_approval

        # Worktree-mode tasks integrate via _phase_integrate under the merge
        # slot, not via _phase_verify's auto-merge remediations.  The agent
        # is expected to leave the slot on its task branch with everything
        # committed; the integration phase rebases + pushes + merges.
        # Worktree-execution spec §6.5.
        is_worktree_task = await self._task_is_worktree_mode(ctx)

        # ── Auto-remediate: commit uncommitted changes ──────────────────
        # Agents frequently forget to commit their work before completing.
        # Rather than reopening the task (which often repeats the same
        # mistake, causing retry loops), commit the changes automatically
        # and continue verification.
        #
        # We pass exclude_plans=False because this is a system-level
        # auto-remediation — we need to commit ALL changes including plan
        # files.  The plan file exclusion is meant to prevent agent-initiated
        # commits from including plans, but auto-remediation must clean up
        # everything to avoid verification failures.
        #
        # We use no_verify=True to bypass pre-commit hooks which can
        # reject the auto-commit (e.g. ruff formatting) and cause the
        # very retry loops we're trying to prevent.
        if has_uncommitted:
            has_uncommitted = await self._auto_remediate_uncommitted(
                workspace,
                task.id,
                current_branch,
                project_id=task.project_id,
                agent_id=ctx.agent.id,
            )

        # ── Auto-remediate: merge to default branch ────────────────────
        # For normal tasks (not intermediate, not PR workflow), the agent
        # should have merged to the default branch.  If they forgot, do
        # it automatically to avoid retry loops.
        #
        # Skipped for worktree-mode tasks: integration is _phase_integrate's
        # job (under the merge slot); the slot deliberately stays on the
        # task branch.  Worktree-execution spec §6.5.
        if (
            not is_worktree_task
            and not is_intermediate
            and not requires_approval
            and not has_uncommitted
            and current_branch != default_branch
            and current_branch == task.branch_name
        ):
            try:
                await self.git._arun(["checkout", default_branch], cwd=workspace)
                await self.git._arun(["merge", current_branch, "--no-edit"], cwd=workspace)
                logger.info(
                    "Task %s: auto-merged branch '%s' into '%s'",
                    task.id,
                    current_branch,
                    default_branch,
                )
                current_branch = default_branch
                # Check for uncommitted changes after merge (e.g. conflicts
                # that resulted in a dirty state)
                has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
                if has_uncommitted:
                    has_uncommitted = await self._auto_remediate_uncommitted(
                        workspace,
                        task.id,
                        current_branch,
                        project_id=task.project_id,
                        agent_id=ctx.agent.id,
                    )
            except Exception as e:
                logger.warning(
                    "Task %s: auto-merge of '%s' into '%s' failed: %s",
                    task.id,
                    current_branch,
                    default_branch,
                    e,
                )
                # Abort merge if it left us in a conflicted state
                try:
                    await self.git._arun(["merge", "--abort"], cwd=workspace)
                except Exception:
                    pass
                # Try to get back to the branch we were on
                try:
                    current_branch = await self.git.aget_current_branch(workspace)
                except Exception:
                    pass
                # Re-check for uncommitted changes — the failed merge or
                # abort may have left the workspace dirty.
                try:
                    has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
                    if has_uncommitted:
                        has_uncommitted = await self._auto_remediate_uncommitted(
                            workspace,
                            task.id,
                            current_branch,
                            project_id=task.project_id,
                            agent_id=ctx.agent.id,
                        )
                except Exception:
                    pass

        # ── Auto-remediate: merge task branch to default ─────────────────
        # For normal tasks (no approval, not intermediate), the agent is
        # expected to merge the task branch into the default branch. Agents
        # frequently forget this step, leaving the workspace on the task
        # branch.  Rather than reopening (which often repeats the mistake),
        # perform the merge automatically.
        #
        # Skipped for worktree-mode tasks — _phase_integrate owns the merge.
        if (
            not is_worktree_task
            and not is_intermediate
            and not requires_approval
            and not has_uncommitted
            and current_branch != default_branch
            and task.branch_name
        ):
            try:
                # Checkout default branch
                await self.git._arun(["checkout", default_branch], cwd=workspace)
                # Merge the task branch into default
                await self.git._arun(["merge", current_branch], cwd=workspace)
                logger.info(
                    "Task %s: auto-merged branch '%s' into '%s'",
                    task.id,
                    current_branch,
                    default_branch,
                )
                # Delete the task branch (best-effort)
                try:
                    await self.git._arun(["branch", "-d", current_branch], cwd=workspace)
                except Exception:
                    pass  # Non-critical — branch delete can fail safely
                current_branch = default_branch
            except Exception as e:
                logger.warning(
                    "Task %s: auto-merge of '%s' into '%s' failed: %s",
                    task.id,
                    current_branch,
                    default_branch,
                    e,
                )
                # Abort any partial merge and switch back to the task branch
                try:
                    await self.git._arun(["merge", "--abort"], cwd=workspace)
                except Exception:
                    pass
                try:
                    await self.git._arun(["checkout", current_branch], cwd=workspace)
                except Exception:
                    pass
                # Re-check for uncommitted changes — the failed merge or
                # abort may have left the workspace dirty.
                try:
                    has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
                    if has_uncommitted:
                        has_uncommitted = await self._auto_remediate_uncommitted(
                            workspace,
                            task.id,
                            current_branch,
                            project_id=task.project_id,
                            agent_id=ctx.agent.id,
                        )
                except Exception:
                    pass

        # ── Auto-remediate: push unpushed commits ───────────────────────
        # After auto-committing/merging (or if agent committed but forgot
        # to push), push to the remote to avoid unnecessary retries.
        if has_remote and not has_uncommitted:
            # Determine the expected branch for this task type
            if is_intermediate or requires_approval:
                expected_push_branch = task.branch_name
            else:
                expected_push_branch = default_branch
            if current_branch == expected_push_branch:
                try:
                    ahead_output = await self.git._arun(
                        ["rev-list", f"origin/{current_branch}..HEAD", "--count"],
                        cwd=workspace,
                    )
                    if ahead_output.strip() != "0":
                        await self.git.apush_branch(
                            workspace,
                            current_branch,
                            event_bus=self.bus,
                            project_id=task.project_id,
                        )
                        logger.info(
                            "Task %s: auto-pushed %s commit(s) on branch '%s'",
                            task.id,
                            ahead_output.strip(),
                            current_branch,
                        )
                except Exception as e:
                    logger.warning(
                        "Task %s: auto-push on branch '%s' failed: %s",
                        task.id,
                        current_branch,
                        e,
                    )

        # ── Final safety net: one last remediation sweep ─────────────────
        # Intermediate steps (merge, merge-abort, push attempts) may have
        # introduced new uncommitted changes that weren't caught by the
        # earlier remediation.  Re-check and remediate one more time before
        # building the failure list.
        try:
            has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
            if has_uncommitted:
                current_branch = await self.git.aget_current_branch(workspace)
                has_uncommitted = await self._auto_remediate_uncommitted(
                    workspace,
                    task.id,
                    current_branch,
                    project_id=task.project_id,
                    agent_id=ctx.agent.id,
                )
        except Exception:
            pass

        # Failures are (message, fixable) tuples. Fixable means the agent can
        # resolve the issue (uncommitted changes, missing merge/push/PR).
        # Unfixable issues (behind origin, diverged history) block immediately.
        failures: list[tuple[str, bool]] = []

        if is_intermediate:
            # Intermediate subtask: should be on task branch with work committed
            if has_uncommitted:
                failures.append(
                    (
                        "You left uncommitted changes in the workspace. "
                        f"Please `git add` and `git commit` your changes on branch "
                        f"`{task.branch_name}`.",
                        True,  # fixable — agent can commit
                    )
                )
            if current_branch != task.branch_name and current_branch != default_branch:
                failures.append(
                    (
                        f"Expected workspace to be on branch `{task.branch_name}` "
                        f"but found `{current_branch}`. "
                        f"Please switch to `{task.branch_name}` and commit your work.",
                        True,  # fixable — agent can switch branches
                    )
                )
        elif requires_approval:
            # PR workflow: should be on task branch, branch pushed, PR exists
            if has_uncommitted:
                failures.append(
                    (
                        "You left uncommitted changes. Please commit them on "
                        f"branch `{task.branch_name}` and push.",
                        True,  # fixable — agent can commit and push
                    )
                )
            # Allow being on default if no changes were made (research task)
            if current_branch == default_branch:
                # No-change task — acceptable, skip PR checks
                pass
            else:
                if has_remote:
                    pr_url = await self.git.afind_open_pr(workspace, task.branch_name)
                    if pr_url:
                        ctx.pr_url = pr_url
                    else:
                        failures.append(
                            (
                                f"No open PR found for branch `{task.branch_name}`. "
                                f"Please push your branch and create a PR: "
                                f"`git push origin {task.branch_name}` then "
                                f"`gh pr create --base {default_branch} "
                                f"--head {task.branch_name}`.",
                                True,  # fixable — agent can push and create PR
                            )
                        )
        elif is_worktree_task:
            # Worktree-mode: agent is expected to leave the slot on its task
            # branch with everything committed.  Integration is
            # _phase_integrate's job under the merge slot — so verification
            # here only checks the local commit state (already covered by
            # auto-remediate above); nothing to fail on.
            if has_uncommitted:
                failures.append(
                    (
                        f"Uncommitted changes remain in the slot on "
                        f"`{task.branch_name}` after auto-remediation.",
                        True,
                    )
                )
            if current_branch != task.branch_name:
                failures.append(
                    (
                        f"Expected the slot to be on `{task.branch_name}` "
                        f"but found `{current_branch}`.",
                        True,
                    )
                )
        else:
            # Normal task / final subtask: should be on default, merged, pushed
            if current_branch == default_branch:
                # On default branch — check it's clean and in sync
                if has_uncommitted:
                    failures.append(
                        (
                            "You left uncommitted changes on "
                            f"`{default_branch}`. Please commit or discard them.",
                            True,  # fixable — agent can commit
                        )
                    )
                if has_remote:
                    try:
                        behind = await self.git._arun(
                            ["rev-list", "HEAD..origin/" + default_branch, "--count"],
                            cwd=workspace,
                        )
                        if behind.strip() != "0":
                            # Auto-pull when the agent made no changes (no-op task).
                            # Being behind origin is not the agent's fault — other
                            # agents may have pushed while this task ran.
                            if not has_uncommitted:
                                try:
                                    await self.git._arun(
                                        ["pull", "--ff-only", "origin", default_branch],
                                        cwd=workspace,
                                    )
                                    logger.info(
                                        "Task %s: auto-pulled %s commit(s) on '%s' "
                                        "(no-change task was behind origin)",
                                        task.id,
                                        behind.strip(),
                                        default_branch,
                                    )
                                except Exception as pull_err:
                                    logger.warning(
                                        "Task %s: auto-pull failed: %s",
                                        task.id,
                                        pull_err,
                                    )
                                    failures.append(
                                        (
                                            f"Local `{default_branch}` is behind "
                                            f"`origin/{default_branch}` and auto-pull "
                                            f"failed. Please `git pull origin "
                                            f"{default_branch}`.",
                                            False,  # unfixable
                                        )
                                    )
                            else:
                                failures.append(
                                    (
                                        f"Local `{default_branch}` is behind "
                                        f"`origin/{default_branch}`. "
                                        f"Please `git pull origin {default_branch}`.",
                                        False,  # unfixable — external changes
                                    )
                                )
                    except GitError:
                        pass
                    try:
                        ahead = await self.git._arun(
                            ["rev-list", "origin/" + default_branch + "..HEAD", "--count"],
                            cwd=workspace,
                        )
                        if ahead.strip() != "0":
                            failures.append(
                                (
                                    f"Local `{default_branch}` has unpushed commits. "
                                    f"Please `git push origin {default_branch}`.",
                                    True,  # fixable — agent can push
                                )
                            )
                    except GitError:
                        pass
            else:
                # Not on default — the agent forgot to merge
                failures.append(
                    (
                        f"Workspace is on branch `{current_branch}` instead of "
                        f"`{default_branch}`. Please merge your work into "
                        f"`{default_branch}` and push:\n"
                        f"  `git checkout {default_branch} && "
                        f"git merge {task.branch_name} && "
                        f"git push origin {default_branch}`",
                        True,  # fixable — agent can merge and push
                    )
                )

        if not failures:
            logger.info("Task %s: git verification passed", task.id)
            return PhaseResult.CONTINUE

        # Separate fixable vs unfixable failures
        fixable = [(msg, f) for msg, f in failures if f]
        unfixable = [(msg, f) for msg, f in failures if not f]
        all_msgs = [msg for msg, _ in failures]

        if unfixable:
            # Unfixable issues present — block immediately, don't waste retries
            unfixable_msgs = [msg for msg, _ in unfixable]
            logger.warning(
                "Task %s: git verification found unfixable issues (%d), blocking: %s",
                task.id,
                len(unfixable),
                "; ".join(unfixable_msgs),
            )
            bullet_list = "\n".join(f"- {msg}" for msg in all_msgs)
            await self._emit_text_notify(
                f"⛔ **Verification Blocked:** Task `{task.id}` — "
                f"git state has unfixable issues (not reopening):\n{bullet_list}",
                project_id=task.project_id,
            )
            ctx.verification_reopened = False
            return PhaseResult.STOP

        # Only fixable issues — attempt to reopen with feedback
        logger.warning(
            "Task %s: git verification failed (%d fixable issues): %s",
            task.id,
            len(fixable),
            "; ".join(all_msgs),
        )
        reopened = await self._reopen_with_verification_feedback(task, fixable)
        ctx.verification_reopened = reopened
        return PhaseResult.STOP

    async def _auto_remediate_uncommitted(
        self,
        workspace: str,
        task_id: str,
        current_branch: str,
        *,
        project_id: str | None = None,
        agent_id: str | None = None,
    ) -> bool:
        """Try to commit uncommitted changes using a robust fallback cascade.

        Returns True if uncommitted changes still remain after all attempts,
        False if the workspace is now clean.

        When *project_id* and/or *agent_id* are given, a ``git.commit`` event
        is emitted on the orchestrator's event bus after a successful commit.

        Fallback cascade:
        0. Abort any in-progress git operations (merge/rebase/cherry-pick)
           and remove stale lock files left by crashed processes.
        1. ``git commit`` with ``--no-verify`` to bypass pre-commit hooks.
        2. ``git stash`` to save changes without committing.
        3. ``git reset --hard HEAD && git clean -fdx`` to discard ALL changes.
        """
        # Attempt 0: Clear any in-progress operations and lock files that
        # would cause all subsequent git operations to fail.  This handles
        # the common case where a killed agent left the workspace in a
        # mid-merge/rebase state or left a stale index.lock.
        try:
            await self.git.aabort_in_progress_operations(workspace)
        except Exception as e:
            logger.warning(
                "Task %s: abort in-progress operations failed: %s",
                task_id,
                e,
            )

        # Attempt 1: commit with --no-verify to bypass pre-commit hooks.
        # Hooks (e.g. ruff formatting) are the most common reason
        # auto-commit fails, causing retry loops.
        try:
            committed = await self.git.acommit_all(
                workspace,
                f"auto-commit: uncommitted changes from task {task_id}",
                exclude_plans=False,
                no_verify=True,
                event_bus=self.bus,
                project_id=project_id,
                agent_id=agent_id,
            )
            if committed:
                logger.info(
                    "Task %s: auto-committed uncommitted changes on branch '%s'",
                    task_id,
                    current_branch,
                )
            # Re-check after commit attempt — handles edge cases where
            # acommit_all returns False but some changes remain (e.g.
            # gitignored files that show in porcelain output).
            has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
            if not has_uncommitted:
                return False
        except Exception as e:
            logger.warning(
                "Task %s: auto-commit (--no-verify) failed: %s",
                task_id,
                e,
            )

        # Attempt 2: stash changes (preserves work, less accessible).
        try:
            await self.git._arun(
                [
                    "stash",
                    "--include-untracked",
                    "-m",
                    f"auto-stash: uncommitted changes from task {task_id}",
                ],
                cwd=workspace,
            )
            logger.info(
                "Task %s: stashed uncommitted changes on branch '%s'",
                task_id,
                current_branch,
            )
            has_uncommitted = await self.git.ahas_uncommitted_changes(workspace)
            if not has_uncommitted:
                return False
        except Exception as e:
            logger.warning(
                "Task %s: auto-stash failed: %s",
                task_id,
                e,
            )

        # Attempt 3: nuclear option — hard-reset and clean everything
        # including ignored files.  Uses git reset --hard HEAD (resets
        # index + working tree for all tracked files) instead of
        # git checkout -- . (which misses staged changes, deleted files,
        # and fails during merge conflicts).
        try:
            clean = await self.git.aforce_clean_workspace(workspace)
            if clean:
                logger.info(
                    "Task %s: force-cleaned workspace on branch '%s'",
                    task_id,
                    current_branch,
                )
                return False
        except Exception as e:
            logger.warning(
                "Task %s: force-clean workspace failed: %s",
                task_id,
                e,
            )

        return True

    async def _reopen_with_verification_feedback(
        self,
        task,
        failures: list[tuple[str, bool]],
    ) -> bool:
        """Reopen a task with git verification feedback.

        Args:
            task: The task to reopen.
            failures: List of (message, fixable) tuples. Only fixable failures
                should be passed here — unfixable ones are handled by the caller.

        Returns True if the task was reopened (transitioned to READY),
        False if max retries were exceeded (task left for caller to block).
        """
        max_retries = self.config.auto_task.max_verification_retries
        # Count previous verification attempts from task_context
        contexts = await self.db.get_task_contexts(task.id)
        retry_count = sum(1 for c in contexts if c.get("type") == "verification_feedback")

        if retry_count >= max_retries:
            logger.warning(
                "Task %s: verification retries exhausted (%d/%d)",
                task.id,
                retry_count,
                max_retries,
            )
            await self._emit_text_notify(
                f"**Verification Failed:** Task `{task.id}` — "
                f"git state is incorrect after {retry_count} retries. "
                f"Manual resolution needed.",
                project_id=task.project_id,
            )
            return False

        # Build feedback message
        bullet_list = "\n".join(f"- {msg}" for msg, _ in failures)
        feedback = (
            f"**Git Verification Feedback (auto-retry "
            f"{retry_count + 1}/{max_retries}):**\n"
            f"The system verified the git state after your work and found "
            f"issues:\n{bullet_list}\n"
            f"Please fix these issues when the task restarts."
        )

        separator = "\n\n---\n"
        updated_description = task.description + separator + feedback

        await self.db.transition_task(
            task.id,
            TaskStatus.READY,
            context="verification_reopen",
            description=updated_description,
            retry_count=0,
            assigned_agent_id=None,
            pr_url=None,
            requires_approval=task.requires_approval,
        )
        await self.db.add_task_context(
            task.id,
            type="verification_feedback",
            label="Git Verification Feedback",
            content=feedback,
        )
        await self._emit_text_notify(
            f"🔄 **Verification reopen:** Task `{task.id}` — "
            f"reopened with feedback (attempt {retry_count + 1}/{max_retries})",
            project_id=task.project_id,
        )
        logger.info(
            "Task %s: reopened for verification (attempt %d/%d)",
            task.id,
            retry_count + 1,
            max_retries,
        )
        return True

    # ── worktree-mode integration ─────────────────────────────────────────

    async def _task_is_worktree_mode(self, ctx: PipelineContext) -> bool:
        """True when the task's workspace kind is in worktree mode.

        Worktree-execution spec §6.5: the integrate phase runs whenever
        the task's project-repo kind is configured as worktree mode —
        including the *base* workspace of that kind, not only the slots.
        Discrimination is by the workspace kind's ``mode`` field; we
        fall back to ``is_slot`` only when the kind lookup fails, so a
        misconfigured install still routes slot workspaces correctly.
        """
        from src.models import KIND_MODE_WORKTREE

        if not getattr(self.config, "worktrees", None) or not self.config.worktrees.enabled:
            return False
        ws_id = getattr(ctx, "workspace_id", None)
        if not ws_id:
            return False
        try:
            ws = await self.db.get_workspace(ws_id)
        except Exception as e:
            logger.warning("worktree-mode detection: get_workspace(%s) failed: %s", ws_id, e)
            return False
        if ws is None:
            return False
        kind_id = getattr(ws, "kind_id", None)
        project_id = getattr(ws, "project_id", None)
        if kind_id and project_id:
            try:
                kind = await self.db.resolve_workspace_kind(project_id, kind_id)
            except Exception as e:
                logger.warning(
                    "worktree-mode detection: resolve_workspace_kind(%s,%s) failed: %s",
                    project_id, kind_id, e,
                )
                return getattr(ws, "is_slot", False)
            if kind is not None:
                return getattr(kind, "mode", None) == KIND_MODE_WORKTREE
        # No kind information — fall back to slot-based detection.
        return getattr(ws, "is_slot", False)

    async def _phase_integrate(self, ctx: PipelineContext) -> PhaseResult:
        """Integration under the per-project merge slot.

        Sequence (worktree-execution spec §6.5, design §4.2/§4.3):

        1. Acquire the merge slot (blocking-with-timeout via lease).
           Emit ``merge.started``.
        2. In the slot: ``git fetch origin``, ``git rebase origin/<default>``.
           Conflict → abort, record ``rejection_reason`` + conflicting
           files, transition the task to BLOCKED, emit ``merge.conflict``,
           release the slot, return STOP.
        3. Push the rebased branch (never ``--force`` to *default*).
        4. In the base: merge the task branch into default and push
           (skipped when ``requires_approval`` — the agent will open a PR
           on the pushed branch).
        5. Emit ``merge.succeeded``, record ``merged_at`` metadata,
           release the slot, return CONTINUE.

        The slot is released in a ``finally`` so a crash between steps
        does not starve every other task on the project.
        """
        task = ctx.task
        workspace = ctx.workspace_path
        default_branch = ctx.default_branch or "main"
        branch = task.branch_name
        if not workspace or not branch:
            return PhaseResult.CONTINUE

        # Plan subtasks share the parent's branch; only the *last* subtask
        # of the plan does integration.  Intermediates commit and stop.
        is_intermediate = task.is_plan_subtask and not await self._is_last_subtask(task)
        if is_intermediate:
            return PhaseResult.CONTINUE

        if task.is_plan_subtask and task.parent_task_id:
            parent = await self.db.get_task(task.parent_task_id)
            requires_approval = parent.requires_approval if parent else task.requires_approval
        else:
            requires_approval = task.requires_approval

        ttl = float(self.config.worktrees.merge_slot_ttl_seconds)
        acquired = await acquire_merge_slot(self.db, task.project_id, task.id, ttl)
        if not acquired:
            logger.info(
                "Task %s: merge slot for project %s is held; deferring integration",
                task.id,
                task.project_id,
            )
            return PhaseResult.STOP

        await self._emit_bus(
            "merge.started",
            {
                "project_id": task.project_id,
                "task_id": task.id,
                "branch": branch,
                "target": default_branch,
                "workspace_id": ctx.workspace_id,
            },
        )
        try:
            # Renew the lease before the potentially-slow rebase.
            await renew_merge_slot(self.db, task.project_id, task.id, ttl)

            # ── Step 2: fetch + rebase in the slot ────────────────────
            has_remote = await self.git.ahas_remote(workspace)
            if has_remote:
                try:
                    await self.git._arun(["fetch", "origin"], cwd=workspace)
                except GitError as e:
                    logger.warning("Task %s: fetch origin failed: %s", task.id, e)

            rebase_target = (
                f"origin/{default_branch}" if has_remote else default_branch
            )
            try:
                await self.git._arun(["switch", branch], cwd=workspace)
                await self.git._arun(["rebase", rebase_target], cwd=workspace)
            except GitError as e:
                # Conflict handling — design §4.3.
                files: list[str] = []
                try:
                    out = await self.git._arun(
                        ["diff", "--name-only", "--diff-filter=U"],
                        cwd=workspace,
                    )
                    files = [line for line in out.splitlines() if line.strip()]
                except GitError:
                    pass
                # Clean rebase state — abort so the slot is usable again.
                try:
                    await self.git._arun(["rebase", "--abort"], cwd=workspace)
                except GitError:
                    pass

                reason = f"merge_conflict: rebase onto {rebase_target} failed: {e}"
                try:
                    await self.db.set_task_meta(task.id, "rejection_reason", reason)
                    await self.db.set_task_meta(task.id, "conflict_files", files)
                except Exception as meta_err:
                    logger.warning(
                        "Task %s: failed to record conflict meta: %s", task.id, meta_err
                    )

                try:
                    await self.db.update_task(task.id, status=TaskStatus.BLOCKED.value)
                except Exception as db_err:
                    logger.warning(
                        "Task %s: failed to transition to BLOCKED: %s", task.id, db_err
                    )

                await self._emit_bus(
                    "merge.conflict",
                    {
                        "project_id": task.project_id,
                        "task_id": task.id,
                        "branch": branch,
                        "target": default_branch,
                        "files": files,
                        "rejection_reason": reason,
                        "workspace_id": ctx.workspace_id,
                    },
                )
                await self._emit_notify(
                    "notify.merge_conflict",
                    MergeConflictEvent(
                        task=build_task_detail(task),
                        branch=branch,
                        target_branch=default_branch,
                        project_id=task.project_id,
                    ),
                )
                return PhaseResult.STOP

            # ── Step 3: push the rebased task branch ──────────────────
            # Task branches are owned by one agent so --force-with-lease
            # is safe for the branch itself.  We NEVER push to default
            # from here; that's the base-side merge below.
            if has_remote:
                # Lease-guarded push (finding #1): the rebase above may
                # have run longer than ``merge_slot_ttl_seconds``; if
                # ``break_expired_merge_slots`` handed the slot to
                # another task in the meantime, we must NOT push — two
                # concurrent pushes would race the remote.  Renew
                # immediately before the push; if the renew fails we no
                # longer own the lease and treat this like contention.
                if not await renew_merge_slot(
                    self.db, task.project_id, task.id, ttl
                ):
                    logger.warning(
                        "Task %s: merge slot lease lost before pushing %s; aborting push",
                        task.id, branch,
                    )
                    return PhaseResult.STOP
                try:
                    await self.git.apush_branch(
                        workspace,
                        branch,
                        force_with_lease=True,
                        event_bus=self.bus,
                        project_id=task.project_id,
                    )
                except Exception as e:
                    logger.warning("Task %s: push %s failed: %s", task.id, branch, e)
                    reason = f"push_failed: {branch}: {e}"
                    try:
                        await self.db.set_task_meta(
                            task.id, "rejection_reason", reason
                        )
                    except Exception as meta_err:
                        logger.warning(
                            "Task %s: failed to record push_failed meta: %s",
                            task.id, meta_err,
                        )
                    await self._emit_notify(
                        "notify.push_failed",
                        PushFailedEvent(
                            task=build_task_detail(task),
                            branch=branch,
                            error_detail=str(e),
                            project_id=task.project_id,
                        ),
                    )
                    return PhaseResult.STOP

            # ── Step 4: local merge in the base (skip for PR workflow) ─
            merged_at: float | None = None
            pr_url = ctx.pr_url
            if not requires_approval:
                base_ws = None
                if ctx.workspace_id:
                    try:
                        slot_ws = await self.db.get_workspace(ctx.workspace_id)
                        if slot_ws is not None and slot_ws.base_workspace_id:
                            base_ws = await self.db.get_workspace(
                                slot_ws.base_workspace_id
                            )
                    except Exception:
                        pass
                base_path = base_ws.workspace_path if base_ws else workspace

                await renew_merge_slot(self.db, task.project_id, task.id, ttl)

                merged = await self.git.amerge_branch(
                    base_path, branch, default_branch
                )
                if not merged:
                    # Should be rare after a successful rebase, but treat
                    # like a conflict rather than force-anything.
                    reason = (
                        f"merge_conflict: base merge of {branch} into "
                        f"{default_branch} failed after rebase"
                    )
                    try:
                        await self.db.set_task_meta(
                            task.id, "rejection_reason", reason
                        )
                        await self.db.update_task(
                            task.id, status=TaskStatus.BLOCKED.value
                        )
                    except Exception:
                        pass
                    await self._emit_bus(
                        "merge.conflict",
                        {
                            "project_id": task.project_id,
                            "task_id": task.id,
                            "branch": branch,
                            "target": default_branch,
                            "files": [],
                            "rejection_reason": reason,
                            "workspace_id": ctx.workspace_id,
                        },
                    )
                    return PhaseResult.STOP

                if has_remote:
                    # Lease-guarded push (finding #1): renew immediately
                    # before pushing to default — if the local merge took
                    # long enough for the lease to expire, another task
                    # may now own the slot and be about to push too.
                    if not await renew_merge_slot(
                        self.db, task.project_id, task.id, ttl
                    ):
                        logger.warning(
                            "Task %s: merge slot lease lost before pushing to %s; aborting",
                            task.id, default_branch,
                        )
                        return PhaseResult.STOP
                    try:
                        # Regular push — no --force here.  The base has
                        # just merged origin/<default> forward.
                        await self.git._arun(
                            ["push", "origin", default_branch], cwd=base_path
                        )
                    except GitError as e:
                        logger.warning(
                            "Task %s: push %s failed: %s", task.id, default_branch, e
                        )
                        reason = f"push_failed: {default_branch}: {e}"
                        try:
                            await self.db.set_task_meta(
                                task.id, "rejection_reason", reason
                            )
                        except Exception as meta_err:
                            logger.warning(
                                "Task %s: failed to record push_failed meta: %s",
                                task.id, meta_err,
                            )
                        return PhaseResult.STOP

                import time as _time

                merged_at = _time.time()
                try:
                    await self.db.set_task_meta(task.id, "merged_at", merged_at)
                except Exception:
                    pass

            # ── Step 5: success ───────────────────────────────────────
            payload: dict = {
                "project_id": task.project_id,
                "task_id": task.id,
                "branch": branch,
                "target": default_branch,
                "workspace_id": ctx.workspace_id,
            }
            if merged_at is not None:
                payload["merged_at"] = merged_at
            if pr_url:
                payload["pr_url"] = pr_url
            await self._emit_bus("merge.succeeded", payload)
            return PhaseResult.CONTINUE
        finally:
            try:
                await release_merge_slot(self.db, task.project_id, task.id)
            except Exception as e:
                logger.warning(
                    "Task %s: releasing merge slot for %s failed: %s",
                    task.id,
                    task.project_id,
                    e,
                )

    async def _emit_bus(self, event_type: str, payload: dict) -> None:
        """Best-effort event emission — never fail the pipeline on a bus hiccup."""
        bus = getattr(self, "bus", None)
        if bus is None:
            return
        try:
            await bus.emit(event_type, payload)
        except Exception as e:
            logger.warning("emit %s failed: %s", event_type, e)
