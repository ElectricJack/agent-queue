"""Finish an explicitly requested stop without reopening blocked or failed work."""
from __future__ import annotations

import asyncio
from pathlib import Path

from src.claim_file import read_claim_file, remove_claim_file_if_matches
from src.database.queries import retained_workspace_queries as queries
from src.git.manager import commit_identity
from src.git.process import ProcessCleanupError
from src.orchestrator.task_checkpoint import capture_checkpoint
from src.orchestrator.worktree_manager import WorktreeSlotManager
from src.sessions.proctable import workspace_quiescent
from src.sessions.provider import SessionHandle


def validate_markers(identity):
    task, ws, session = (identity[key] for key in ("task", "workspace", "session"))
    claim = read_claim_file(ws["workspace_path"])
    sentinel = WorktreeSlotManager.read_sentinel(ws["workspace_path"])
    if (claim is None or claim.get("task_id") != task["id"]
            or claim.get("claim_epoch") != task["claim_epoch"]
            or claim.get("session_id") != session["id"]
            or sentinel is None or sentinel.workspace_id != ws["id"]
            or sentinel.base_workspace_id != ws["base_workspace_id"]
            or sentinel.project_id != task["project_id"] or sentinel.task_id != task["id"]
            or sentinel.branch != task["branch_name"].removeprefix("refs/heads/")):
        raise queries.RetainedWorkspaceError("Claim or source sentinel is missing or changed")


async def stop_retained_workspace(orch, task_id):
    """Return an actionable refusal, or release the exact stopped, preserved holder."""
    try:
        saved = await queries.begin(orch.db, task_id)
        if not saved["cleanup_pending"]:
            return None
        identity = saved["identity"]
        session, ws, task = (identity[key] for key in ("session", "workspace", "task"))
        # There is no durable completion proof for a preparation lost across a
        # restart. Never let this explicit retry erase its uncertainty.
        if any(key[1] == task_id for key in orch.claim_preparations):
            raise queries.RetainedWorkspaceError("Claim preparation still owns source cleanup")
        background = orch._running_tasks.get(task_id)
        if background is not None and not background.done():
            raise queries.RetainedWorkspaceError("Execution cleanup is still running")
        if background is not None and not background.cancelled():
            error = background.exception()
            if isinstance(error, ProcessCleanupError):
                raise error
        async with orch._pool_teardown_lock(session["id"]):
            async with orch.db.immediate() as conn:
                # Keep identity and acquisition locks through external proofs
                # and final release; a new claim/job cannot enter this slot.
                await queries.validate(orch.db, conn, task_id, saved)
                validate_markers(identity)
                path = ws["workspace_path"]
                roots = [await orch.git._arun(
                    ["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd,
                ) for cwd in (path, identity["base"]["workspace_path"])]
                if Path(roots[0]).resolve() != Path(roots[1]).resolve():
                    raise queries.RetainedWorkspaceError("Slot belongs to a different base repository")
                origin = await orch.git._arun(["remote", "get-url", "origin"], cwd=path)
                if origin != identity["repository"]["url"]:
                    raise queries.RetainedWorkspaceError("Source repository origin changed")
                git_dir = await orch.git._arun(
                    ["rev-parse", "--absolute-git-dir"], cwd=path,
                )
                provider = orch.session_providers.create(session["provider"], orch.config)
                handle = SessionHandle(session["name"], session["provider"], session["instance_token"])
                token_store = getattr(orch, "token_store", None)
                if token_store is not None:
                    await token_store.revoke_session(session["id"])
                await asyncio.wait_for(provider.stop(handle, grace=2.0), timeout=10)
                if await asyncio.wait_for(provider.confirm_stopped(handle), timeout=10) is not True:
                    raise queries.RetainedWorkspaceError("Exact provider termination is unproved")
                adapter = orch._adapters.get(session["agent_id"])
                if adapter is not None:
                    await asyncio.wait_for(adapter.stop(), timeout=10)

                async def validate():
                    await queries.validate(orch.db, conn, task_id, saved)
                    validate_markers(identity)
                    if await workspace_quiescent((path, git_dir), session["instance_token"]) is not True:
                        raise queries.RetainedWorkspaceError("A process still uses the retained source")

                await validate()
                head = await orch.git._arun(["rev-parse", "--verify", "HEAD"], cwd=path)
                branch = task["branch_name"].removeprefix("refs/heads/")
                branch_ref = "refs/heads/" + branch
                branch_head = await orch.git._arun(["rev-parse", "--verify", branch_ref], cwd=path)
                if head != branch_head:
                    raise queries.RetainedWorkspaceError("HEAD differs from the proved task branch tip")
                captured = None

                async def save(checkpoint):
                    nonlocal captured
                    await validate()
                    if await orch.git._arun(["rev-parse", "--verify", branch_ref], cwd=path) != head:
                        raise queries.RetainedWorkspaceError("Task branch changed during preservation")
                    captured = checkpoint | {"retained_stop_branch": {
                        "task_id": task_id, "branch": branch, "head": head,
                        "source": roots[0], "claim": identity["claim"],
                    }}
                    await queries.save_checkpoint(orch.db, conn, task_id, saved, captured)

                project = await orch.db.get_project(task["project_id"])
                with commit_identity(orch.git.resolve_commit_identity(project)):
                    await capture_checkpoint(
                        orch.db, orch.git, task_id, path, validate=validate, save=save,
                        expected_branch=branch,
                    )
                if captured is None:
                    raise queries.RetainedWorkspaceError("No full Git checkpoint was captured")
                await validate()
                # Same-HEAD detach preserves index/files and unpushed commits;
                # the full checkpoint remains reachable in the shared base.
                await orch.git._arun(["switch", "--detach", head], cwd=path)
                await validate()
                if await asyncio.wait_for(provider.confirm_stopped(handle), timeout=10) is not True:
                    raise queries.RetainedWorkspaceError("Provider termination changed before release")
                await queries.finish(orch.db, conn, task_id, saved)
            # The database release must commit first. The exact epoch check
            # makes a successor's marker untouchable if it already acquired.
            remove_claim_file_if_matches(
                path, task_id, task["claim_epoch"], session_id=session["id"],
            )
        orch._task_attachments.pop(task_id, None)
        orch._adapters.pop(session["agent_id"], None)
        return None
    except ProcessCleanupError:
        await queries.retain_cleanup_uncertainty(orch.db, task_id, saved)
        return "Stop child termination is unproved; source and recovery hold were retained."
    except Exception as exc:
        from src.preparation_failure import safe_text

        reason = safe_text(str(exc) or type(exc).__name__)
        return (f"Retained workspace stop refused: {reason}. Source and ownership were retained. "
                f"Inspect the existing recovery owner and retry `aq task stop {task_id}` "
                "after its missing proof is resolved; do not clear its claim or locks.")
