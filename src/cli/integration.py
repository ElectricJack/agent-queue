"""Hand-crafted operational controls for hierarchical integration trains.

The CLI owns presentation only.  Every command delegates to the daemon's
existing generic execute endpoint, where project scope and LOCAL-operator
authority are enforced.
"""

from __future__ import annotations

from typing import Any

import click

from .app import _get_client, _handle_errors, _run, cli
from .claim_epoch import claim_epoch_option, resolve_claim_epoch
from .envelope import emit


def _execute(ctx: click.Context, command: str, args: dict[str, Any]) -> None:
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _request():
        async with _get_client(api_url) as client:
            return await client.execute(command, args)

    emit(ctx, _run(_request()), entity="integration")


@cli.group("integration")
def integration() -> None:
    """Inspect and control hierarchical integration trains."""


@integration.command("status")
@click.argument("project_id")
@click.option("--control-only", is_flag=True, help="Read durable control state without readiness observations.")
@click.pass_context
@_handle_errors
def integration_status(ctx: click.Context, project_id: str, control_only: bool) -> None:
    """Show rollout, readiness, active work, and cleanup for PROJECT_ID."""
    args: dict[str, Any] = {"project_id": project_id}
    if control_only:
        args["control_only"] = True
    _execute(ctx, "integration_status", args)


@integration.command("record-noop")
@click.argument("child_task_id")
@click.option("--expected-head-sha", required=True, help="Exact child checkpoint commit.")
@click.pass_context
@_handle_errors
def integration_record_noop(
    ctx: click.Context, child_task_id: str, expected_head_sha: str
) -> None:
    """Record a verified no-code receipt for a completed child task."""
    _execute(
        ctx,
        "integration_record_noop",
        {"child_task_id": child_task_id, "expected_head_sha": expected_head_sha},
    )


@integration.command("resolve-candidate-member")
@click.option("--resolved-head-sha", required=True)
@click.option("--resolved-tree-sha", required=True)
@click.option(
    "--repair-commit-sha",
    "repair_commit_shas",
    multiple=True,
    required=True,
    help="Exact repair commit in oldest-to-newest order; repeat for every commit.",
)
@claim_epoch_option
@click.pass_context
@_handle_errors
def integration_resolve_candidate_member(
    ctx: click.Context,
    resolved_head_sha: str,
    resolved_tree_sha: str,
    repair_commit_shas: tuple[str, ...],
    claim_epoch: int | None,
) -> None:
    """Resolve the candidate member assigned to this repair session.

    The daemon derives the active batch, revision, member, operation, partial
    head, and branch fence from the authenticated session.  This command never
    accepts those authority-bearing values from the caller.
    """
    args: dict[str, Any] = {
        "resolved_head_sha": resolved_head_sha,
        "resolved_tree_sha": resolved_tree_sha,
        "repair_commit_shas": list(repair_commit_shas),
    }
    resolved_epoch = resolve_claim_epoch(claim_epoch)
    if resolved_epoch is not None:
        args["claim_epoch"] = resolved_epoch
    _execute(ctx, "integration_resolve_candidate_member", args)


@integration.command("flush")
@click.argument("project_id")
@click.pass_context
@_handle_errors
def integration_flush(ctx: click.Context, project_id: str) -> None:
    """Request an immediate eligibility pass or train sweep for PROJECT_ID."""
    _execute(ctx, "integration_flush", {"project_id": project_id})


@integration.command("eject")
@click.option("--batch-id", required=True)
@click.option("--task-id", required=True)
@click.option("--reason", required=True)
@click.pass_context
@_handle_errors
def integration_eject(ctx: click.Context, batch_id: str, task_id: str, reason: str) -> None:
    """Remove one epic from a sealed batch while retaining its approval."""
    _execute(
        ctx,
        "integration_eject",
        {"batch_id": batch_id, "task_id": task_id, "reason": reason},
    )


@integration.command("enable")
@click.argument("project_id")
@click.option(
    "--mode",
    type=click.Choice(["disabled", "observe", "hierarchy", "train"]),
    required=True,
)
@click.option("--expected-generation", type=click.IntRange(min=0), required=True)
@click.option("--reason", required=True)
@click.option("--waiver-id")
@click.option("--interval-seconds", type=click.IntRange(min=1))
@click.pass_context
@_handle_errors
def integration_enable(
    ctx: click.Context,
    project_id: str,
    mode: str,
    expected_generation: int,
    reason: str,
    waiver_id: str | None,
    interval_seconds: int | None,
) -> None:
    """CAS PROJECT_ID to MODE using the generation reported by status."""
    args: dict[str, Any] = {
        "project_id": project_id,
        "mode": mode,
        "expected_generation": expected_generation,
        "reason": reason,
    }
    if waiver_id is not None:
        args["waiver_id"] = waiver_id
    if interval_seconds is not None:
        if mode != "train":
            raise click.UsageError("--interval-seconds is only valid with --mode train")
        args["interval_seconds"] = interval_seconds
    _execute(ctx, "integration_enable", args)


@integration.command("reconcile-unmaterialized")
@click.argument("project_id")
@click.option("--expected-generation", type=click.IntRange(min=0), required=True)
@click.option("--reason", required=True)
@click.pass_context
@_handle_errors
def integration_reconcile_unmaterialized(
    ctx: click.Context, project_id: str, expected_generation: int, reason: str
) -> None:
    """Bind safe pre-rollout tasks and reserve their hierarchy origins."""
    _execute(
        ctx,
        "integration_reconcile_unmaterialized",
        {
            "project_id": project_id,
            "expected_generation": expected_generation,
            "reason": reason,
        },
    )


@integration.command("waive-history")
@click.argument("project_id")
@click.option("--reason", required=True)
@click.option("--blocker-digest", required=True)
@click.pass_context
@_handle_errors
def integration_waive_history(
    ctx: click.Context,
    project_id: str,
    reason: str,
    blocker_digest: str,
) -> None:
    """Waive only the exact historical blockers reported for PROJECT_ID."""
    _execute(
        ctx,
        "integration_waive_history",
        {
            "project_id": project_id,
            "reason": reason,
            "blocker_digest": blocker_digest,
        },
    )


@integration.command("resume")
@click.argument("operation_id")
@click.pass_context
@_handle_errors
def integration_resume(ctx: click.Context, operation_id: str) -> None:
    """Resume a safe, human-required integration OPERATION_ID."""
    _execute(ctx, "integration_resume", {"operation_id": operation_id})


@integration.command("abort")
@click.argument("operation_id")
@click.option("--reason", required=True)
@click.pass_context
@_handle_errors
def integration_abort(ctx: click.Context, operation_id: str, reason: str) -> None:
    """Abort a safe, human-required integration OPERATION_ID."""
    _execute(ctx, "integration_abort", {"operation_id": operation_id, "reason": reason})


@integration.command("retry-cleanup")
@click.argument("batch_id")
@click.pass_context
@_handle_errors
def integration_retry_cleanup(ctx: click.Context, batch_id: str) -> None:
    """Requeue the exact safe cleanup items for BATCH_ID.

    A promoted batch whose cleanup never materialized has no item to requeue;
    for it, this materializes the cleanup, which the daemon then runs.
    """
    _execute(ctx, "integration_retry_cleanup", {"batch_id": batch_id})


@integration.command("release-owner")
@click.option("--task-id")
@click.option("--owner-row-id")
@click.option("--dry-run", is_flag=True)
@click.pass_context
@_handle_errors
def integration_release_owner(
    ctx: click.Context, task_id: str | None, owner_row_id: str | None, dry_run: bool
) -> None:
    """Release a stranded branch owner after proving its writer and branch safe."""
    if (task_id is None) == (owner_row_id is None):
        raise click.UsageError("provide exactly one of --task-id or --owner-row-id")
    args: dict[str, Any] = {"dry_run": dry_run}
    if task_id is not None:
        args["task_id"] = task_id
    else:
        args["owner_row_id"] = owner_row_id
    _execute(ctx, "integration_release_owner", args)


@integration.command("reserve-owner")
@click.option("--task-id", required=True)
@click.pass_context
@_handle_errors
def integration_reserve_owner(ctx: click.Context, task_id: str) -> None:
    """Restore a stopped train task's missing canonical branch reservation."""
    _execute(ctx, "integration_reserve_owner", {"task_id": task_id})


@integration.command("release-stale-owners")
@click.option("--project-id", required=True)
@click.option("--dry-run", is_flag=True, help="Report what would be released; change nothing.")
@click.option(
    "--older-than",
    help="Only rows unchanged for at least this long, e.g. 30m, 4h, 2d.",
)
@click.pass_context
@_handle_errors
def integration_release_stale_owners(
    ctx: click.Context, project_id: str, dry_run: bool, older_than: str | None
) -> None:
    """Release a project's stale reserved branch owners that are provably safe.

    A row is released only when it is reserved (names no writer), its owner
    finished and nothing still relies on its fence, and origin proves its work
    safe: the branch tip is on the default branch, or the branch is gone and
    its owner is terminal.  An expired lease of a finished batch is released
    too.  Every other row is listed with the reason it was kept; an attached
    or handoff_pending row belongs to `aq integration release-owner`.  Safe to
    repeat.
    """
    args: dict[str, Any] = {"project_id": project_id, "dry_run": dry_run}
    if older_than is not None:
        args["older_than"] = older_than
    _execute(ctx, "integration_release_stale_owners", args)


@integration.command("adopt-legacy-deliveries")
@click.option("--project-id", required=True)
@click.option("--dry-run", is_flag=True, help="Report what would be adopted; change nothing.")
@click.option(
    "--supersede",
    metavar="TASK_ID",
    help="This child's work was re-delivered as the --by commit (needs --by and --reason).",
)
@click.option(
    "--by",
    "by",
    metavar="SHA",
    help="With --supersede: the commit on the default branch that re-delivered the work.",
)
@click.option(
    "--retire",
    "retire",
    multiple=True,
    metavar="TASK_ID",
    help="Record this child's work as abandoned; deletes nothing (repeatable; needs --reason).",
)
@click.option(
    "--accept",
    "accept",
    multiple=True,
    metavar="TASK_ID",
    help="Record this unprovable child as operator-accepted (repeatable; needs --reason).",
)
@click.option("--reason", help="Audit reason; required with --supersede, --retire or --accept.")
@click.pass_context
@_handle_errors
def integration_adopt_legacy_deliveries(
    ctx: click.Context,
    project_id: str,
    dry_run: bool,
    supersede: str | None,
    by: str | None,
    retire: tuple[str, ...],
    accept: tuple[str, ...],
    reason: str | None,
) -> None:
    """Adopt delivered children of parents that finished outside the train.

    Observe-mode status reports `missing_receipt` for a terminal child of a
    terminal parent with no current parent collection: such a parent is never
    collected again, so no train receipt can ever exist.  Status already
    accepts a child the development publisher delivered to the default branch.
    For every other flagged child this fetches the designated repository and
    records a legacy delivery when a development delivery's commit or the
    child's branch tip is an ancestor of the default branch, or when merging
    the child's work into it changes nothing (re-delivered under other
    commits).  It lists every child it cannot prove with the reason and, under
    `undelivered`, what merging its work would still change.

    Decide those one child at a time, with a reason: `--supersede TASK_ID --by
    SHA` when SHA on the default branch re-delivered the work, `--retire
    TASK_ID` when the work was abandoned (nothing is deleted), or `--accept
    TASK_ID` to accept it without proof.  Safe to repeat.
    """
    if (supersede is None) != (by is None):
        raise click.UsageError("--supersede and --by go together")
    if (supersede or retire or accept) and not reason:
        raise click.UsageError("--supersede, --retire and --accept require --reason")
    args: dict[str, Any] = {"project_id": project_id, "dry_run": dry_run}
    if supersede is not None:
        args["supersede"] = {supersede: by}
    if retire:
        args["retire"] = list(retire)
    if accept:
        args["accept"] = list(accept)
    if reason is not None:
        args["reason"] = reason
    _execute(ctx, "integration_adopt_legacy_deliveries", args)


@integration.command("bind-legacy-repositories")
@click.argument("project_id")
@click.option("--apply", is_flag=True, help="Bind proven tasks; default is a dry run.")
@click.option("--reason", help="Required audit reason when applying bindings.")
@click.pass_context
@_handle_errors
def integration_bind_legacy_repositories(
    ctx: click.Context, project_id: str, apply: bool, reason: str | None
) -> None:
    """List terminal hierarchy members with repository delivery proof, then bind them."""
    if apply and not reason:
        raise click.UsageError("--apply requires --reason")
    _execute(ctx, "integration_bind_legacy_repositories", {
        "project_id": project_id, "dry_run": not apply, "reason": reason,
    })


@integration.command("close-delivered-pr")
@click.argument("project_id")
@click.argument("pr_number", type=click.IntRange(min=1))
@click.option("--apply", is_flag=True, help="Close the proven PR; default is a dry run.")
@click.option("--head", "expected_head_sha", help="Exact head reported by the dry run.")
@click.option("--reason", help="Audit reason required when applying.")
@click.pass_context
@_handle_errors
def integration_close_delivered_pr(
    ctx: click.Context, project_id: str, pr_number: int, apply: bool,
    expected_head_sha: str | None, reason: str | None,
) -> None:
    """Close open PR PR_NUMBER only once Git proves its work is on the default branch.

    GitHub closes a PR as merged when its exact head lands; this covers work
    delivered under other commits and untracked branches. The dry run reports
    `would_close` with the proof (`ancestor`, `patch_equivalent`,
    `content_equivalent`), `undelivered` with what merging would still change,
    or `nothing_to_close`. `--apply` needs that head and a reason; it posts one
    proof comment and closes the PR. Tasks and branches are never changed.
    """
    if apply and not (expected_head_sha and reason):
        raise click.UsageError("--apply requires --head and --reason")
    args: dict[str, Any] = {
        "project_id": project_id, "pr_number": pr_number, "dry_run": not apply,
    }
    if expected_head_sha is not None:
        args["expected_head_sha"] = expected_head_sha
    if reason is not None:
        args["reason"] = reason
    _execute(ctx, "integration_close_delivered_pr", args)


@integration.command("clear-stale-request")
@click.argument("project_id")
@click.option("--apply", is_flag=True, help="Release the request; default is a dry run.")
@click.option(
    "--request-id",
    help="The outstanding request the dry run reported; required with --apply.",
)
@click.option("--reason", help="Required audit reason when applying.")
@click.pass_context
@_handle_errors
def integration_clear_stale_request(
    ctx: click.Context,
    project_id: str,
    apply: bool,
    request_id: str | None,
    reason: str | None,
) -> None:
    """Say whether PROJECT_ID's outstanding sweep request can still end; free it if not.

    A train request is freed only by releasing its promoted batch.  When its
    batch was aborted, is gone, or promoted without its lease, every flush
    answers `coalesced` and no sweep runs.  The dry run prints the verdict
    (`stale`, `unsealed`, `blocked`, `active`, `in_flight`, `none`) and the
    request id; `--apply` needs that id and a reason, and refuses `blocked`.
    """
    if apply and not (request_id and reason):
        raise click.UsageError("--apply requires --request-id and --reason")
    args: dict[str, Any] = {"project_id": project_id, "dry_run": not apply}
    if request_id is not None:
        args["expected_request_id"] = request_id
    if reason is not None:
        args["reason"] = reason
    _execute(ctx, "integration_clear_stale_request", args)


@integration.command("redrive-root")
@click.argument("task_id")
@click.option("--apply", is_flag=True, help="Open the missing PR; default is a dry run.")
@click.option(
    "--head",
    "expected_head_sha",
    help="The head the dry run reported; required with --apply.",
)
@click.option("--reason", help="Required audit reason when applying.")
@click.pass_context
@_handle_errors
def integration_redrive_root(
    ctx: click.Context,
    task_id: str,
    apply: bool,
    expected_head_sha: str | None,
    reason: str | None,
) -> None:
    """Say why completed train root TASK_ID has no pull request; open it if it should.

    The train seats a root only once it has a PR and an approved review of its
    exact head.  The dry run reads the root's checkpoint, the remote branch tip
    and whether that head is already on the default branch, and answers
    `would_open`, `nothing_to_redrive`, `blocked` or `not_eligible` with the
    reason and the head.  `--apply` needs that head and a reason, and opens
    the PR only for it. A wrongly BLOCKED collecting root instead reports
    `would_collect`; applying restores PAUSED under its existing collector
    fence and episode. Operator holds and terminal repair failures stay guarded.
    """
    if apply and not (expected_head_sha and reason):
        raise click.UsageError("--apply requires --head and --reason")
    args: dict[str, Any] = {"task_id": task_id, "dry_run": not apply}
    if expected_head_sha is not None:
        args["expected_head_sha"] = expected_head_sha
    if reason is not None:
        args["reason"] = reason
    _execute(ctx, "integration_redrive_root", args)


@integration.command("materialize-root")
@click.argument("task_id")
@click.option("--apply", is_flag=True, help="Record the proven root identity.")
@click.option("--head", "expected_head_sha", help="Exact head reported by the dry run.")
@click.option("--reason", help="Audit reason required when applying.")
@click.pass_context
@_handle_errors
def integration_materialize_root(
    ctx: click.Context, task_id: str, apply: bool,
    expected_head_sha: str | None, reason: str | None,
) -> None:
    """Prove a completed legacy train root's PR head and record its missing identity."""
    if apply and not (expected_head_sha and reason):
        raise click.UsageError("--apply requires --head and --reason")
    args: dict[str, Any] = {"task_id": task_id, "dry_run": not apply}
    if expected_head_sha is not None:
        args["expected_head_sha"] = expected_head_sha
    if reason is not None:
        args["reason"] = reason
    _execute(ctx, "integration_materialize_root", args)


@integration.command("authorize-root")
@click.argument("task_id")
@click.option("--apply", is_flag=True, help="Record the authorization; default is a dry run.")
@click.option(
    "--head",
    "expected_head_sha",
    help="The head the dry run reported; required with --apply.",
)
@click.option("--reason", help="Required audit reason when applying.")
@click.pass_context
@_handle_errors
def integration_authorize_root(
    ctx: click.Context, task_id: str, apply: bool,
    expected_head_sha: str | None, reason: str | None,
) -> None:
    """Authorize completed train root TASK_ID's exact source for delivery.

    Root `admission: authorized` admits feature and bugfix roots and the ids the
    policy lists. This records an explicit authorization for one other root's
    exact source (base, head and checkpoint generation) without changing the
    policy, its generation or the task's type, so the train keeps running.
    The dry run reports the source and answers `would_authorize`,
    `already_authorized`, `blocked` or `not_eligible`. `--apply` needs the
    reported head and a reason. Holds, open gates, a rejected review and source
    CI still bind; a new head needs a new authorization.
    """
    if apply and not (expected_head_sha and reason):
        raise click.UsageError("--apply requires --head and --reason")
    args: dict[str, Any] = {"task_id": task_id, "dry_run": not apply}
    if expected_head_sha is not None:
        args["expected_head_sha"] = expected_head_sha
    if reason is not None:
        args["reason"] = reason
    _execute(ctx, "integration_authorize_root", args)


@integration.command("redrive-child")
@click.argument("task_id")
@click.option(
    "--apply", is_flag=True, help="Advance the child into assembly; default is a dry run."
)
@click.option(
    "--head",
    "expected_head_sha",
    help="The head the dry run reported; required with --apply.",
)
@click.option("--reason", help="Required audit reason when applying.")
@click.pass_context
@_handle_errors
def integration_redrive_child(
    ctx: click.Context,
    task_id: str,
    apply: bool,
    expected_head_sha: str | None,
    reason: str | None,
) -> None:
    """Say why completed child TASK_ID was never assembled into its parent; advance it.

    A collecting parent assembles a COMPLETED child only once approved evidence
    pins the child's exact checkpoint head.  The dry run reads the child and its
    parent, proves the head from Git (the remote branch tip, descended from the
    child's origin base) and answers `would_advance`, `nothing_to_redrive`,
    `blocked` or `not_eligible` with the reason and the head.  `--apply` needs
    that head and a reason: it records approved evidence for exactly that head
    and queues the parent's collection.
    """
    if apply and not (expected_head_sha and reason):
        raise click.UsageError("--apply requires --head and --reason")
    args: dict[str, Any] = {"task_id": task_id, "dry_run": not apply}
    if expected_head_sha is not None:
        args["expected_head_sha"] = expected_head_sha
    if reason is not None:
        args["reason"] = reason
    _execute(ctx, "integration_redrive_child", args)


@integration.command("reopen-collection")
@click.argument("task_id")
@click.option(
    "--apply", is_flag=True, help="Reopen the parent's collection; default is a dry run."
)
@click.option(
    "--head",
    "expected_head_sha",
    help="The parent branch head the dry run reported; required with --apply.",
)
@click.option("--reason", help="Required audit reason when applying.")
@click.pass_context
@_handle_errors
def integration_reopen_collection(
    ctx: click.Context,
    task_id: str,
    apply: bool,
    expected_head_sha: str | None,
    reason: str | None,
) -> None:
    """Reopen parent TASK_ID's collection after cancellation or failed verification.

    The parent keeps its episode, so delivered receipts stay bound as recorded.
    The dry run proves the parent branch tip is the recorded collection head and
    that no writer, hold or unresolved push remains, then answers
    `would_reopen`, `nothing_to_reopen`, `ambiguous`, `blocked` or
    `not_eligible` with the head, receipts, owner, delegates and any conflict.
    `--apply` needs that head and a reason: it reclaims the collector fence for
    the same operation and, for one current conflict, opens a fresh repair stage
    that files a new delegate. Archived delegates are never restored.
    A settled failed aggregate verifier requires a completed additional child
    fix. Recovery preserves its failed completion, advances the checkpoint
    generation and creates a fresh verifier after collecting the fix.
    A suspended producer with a confirmed detached workspace is transferred to
    its existing collector operation after checking holders, holds, gates and
    unresolved writes. Collection reconciliation performs the same recovery.
    """
    if apply and not (expected_head_sha and reason):
        raise click.UsageError("--apply requires --head and --reason")
    args: dict[str, Any] = {"task_id": task_id, "dry_run": not apply}
    if expected_head_sha is not None:
        args["expected_head_sha"] = expected_head_sha
    if reason is not None:
        args["reason"] = reason
    _execute(ctx, "integration_reopen_collection", args)


@integration.command("rebind-reused-identity")
@click.option("--task-id", required=True)
@click.option("--apply", is_flag=True, help="Rebind the identity; default is a dry run.")
@click.option(
    "--origin-id",
    "origin_ids",
    multiple=True,
    help="An inherited origin the dry run reported; --apply needs every one.",
)
@click.option(
    "--discard-tip",
    "discard_tips",
    multiple=True,
    help="An unproven predecessor commit the dry run reported, explicitly abandoned.",
)
@click.option("--reason", help="Required audit reason when applying.")
@click.pass_context
@_handle_errors
def integration_rebind_reused_identity(
    ctx: click.Context,
    task_id: str,
    apply: bool,
    origin_ids: tuple[str, ...],
    discard_tips: tuple[str, ...],
    reason: str | None,
) -> None:
    """Prove a task's inherited branch origin was delivered; retire it if so.

    A task minted onto a deleted task's name inherited its branch origin and
    checkpoint (`aq doctor --check integration.reused_task_identity`).  The dry
    run checks every predecessor commit (origin base, checkpoint, the ref's
    tip) against the default branch and lists what it cannot prove: a live
    writer, a held owner, dependent integration history, a hierarchy/train
    project, or a commit not on the default branch.  `--apply` needs every
    `--origin-id` the dry run reported and a reason; it retires the origins
    (kept for audit), moves the checkpoint into an audit event and touches no
    branch.  `--discard-tip SHA` abandons one exact unproven commit.
    """
    if apply and not (origin_ids and reason):
        raise click.UsageError("--apply requires --origin-id and --reason")
    args: dict[str, Any] = {"task_id": task_id, "dry_run": not apply}
    if origin_ids:
        args["expected_origin_ids"] = list(origin_ids)
    if discard_tips:
        args["discard_tips"] = list(discard_tips)
    if reason is not None:
        args["reason"] = reason
    _execute(ctx, "integration_rebind_reused_identity", args)


@integration.command("rebind-repair")
@click.option("--task-id", required=True)
@click.option("--dry-run/--apply", default=True, help="Prove only, or reserve the proven candidate.")
@click.option("--head", "expected_head_sha", help="Exact candidate head reported by dry-run; required with --apply.")
@click.pass_context
@_handle_errors
def integration_rebind_repair(
    ctx: click.Context, task_id: str, dry_run: bool, expected_head_sha: str | None
) -> None:
    """Prove a live delegate's candidate against its current conflict intent.

    Apply refreshes a stale stage and reserves the exact candidate under the
    current intent. The attached repair session then performs the fenced push.
    """
    if not dry_run and not expected_head_sha:
        raise click.UsageError("--apply requires --head from dry-run")
    args: dict[str, Any] = {"task_id": task_id, "dry_run": dry_run}
    if expected_head_sha is not None:
        args["expected_head_sha"] = expected_head_sha
    _execute(ctx, "integration_rebind_repair", args)


@integration.command("recover-parent-head")
@click.argument("operation_id")
@click.option("--head", "head_sha", required=True, help="Exact published repair commit SHA.")
@click.option("--dry-run/--apply", default=True)
@click.option("--episode", "expected_episode_id", help="Episode from preview.")
@click.option("--generation", "expected_generation", type=int, help="Generation from preview.")
@click.option("--stage", "expected_stage", type=int, help="Repair stage from preview.")
@click.option("--fence", "expected_fence_token", type=int, help="Current owner fence from preview.")
@click.option("--reason", help="Operator reason; required with --apply.")
@click.pass_context
@_handle_errors
def integration_recover_parent_head(
    ctx, operation_id, head_sha, dry_run, expected_episode_id, expected_generation,
    expected_stage, expected_fence_token, reason,
):
    """Prove a completed parent repair extends the original child receipts.

    Apply advances the aggregate checkpoint and requires fresh verification.
    """
    if not dry_run and (
        not expected_episode_id or expected_generation is None or expected_stage is None
        or expected_fence_token is None or not (reason or "").strip()
    ):
        raise click.UsageError("--apply requires --episode, --generation, --stage, --fence and --reason")
    args = {"operation_id": operation_id, "head_sha": head_sha, "dry_run": dry_run}
    for key, value in (("expected_episode_id", expected_episode_id),
                       ("expected_generation", expected_generation), ("expected_stage", expected_stage),
                       ("expected_fence_token", expected_fence_token), ("reason", reason)):
        if value is not None:
            args[key] = value
    _execute(ctx, "integration_recover_parent_head", args)


@integration.command("recover-preserved-repair")
@click.argument("operation_id")
@click.option("--intent", "intent_id", required=True, help="Exact current conflict intent.")
@click.option("--candidate", "candidate_sha", required=True, help="Exact preserved commit SHA.")
@click.option("--dry-run/--apply", default=True, help="Prove only, or publish the proven resolution.")
@click.option("--stage", "expected_stage", type=int, help="Stage from preview.")
@click.option("--released-fence", "expected_released_fence", type=int, help="Released fence from preview.")
@click.option("--reason", help="Operator reason; required with --apply.")
@click.pass_context
@_handle_errors
def integration_recover_preserved_repair(
    ctx, operation_id, intent_id, candidate_sha, dry_run,
    expected_stage, expected_released_fence, reason,
):
    """Recover a completed parent resolution preserved by owner recovery.

    Keeps exhausted deadlines and consumed attempts. Parent verification and
    human gates still apply. No new repair stage or writer is started.
    """
    if not dry_run and (expected_stage is None or expected_released_fence is None or not reason):
        raise click.UsageError("--apply requires --stage, --released-fence and --reason")
    args = {"operation_id": operation_id, "intent_id": intent_id,
            "candidate_sha": candidate_sha, "dry_run": dry_run}
    for key, value in (("expected_stage", expected_stage),
                       ("expected_released_fence", expected_released_fence), ("reason", reason)):
        if value is not None:
            args[key] = value
    _execute(ctx, "integration_recover_preserved_repair", args)


@integration.command("rebind-detached-repair")
@click.argument("operation_id")
@click.option("--dry-run/--apply", default=True, help="Prove only, or rebind the proven stage.")
@click.option("--stage", "expected_stage", type=int, help="Stage ordinal reported by dry-run.")
@click.option(
    "--remote-head", "expected_remote_head_sha",
    help="Published parent head reported by dry-run; required with --apply.",
)
@click.option("--reason", help="Why the stage is rebound; required with --apply.")
@click.pass_context
@_handle_errors
def integration_rebind_detached_repair(
    ctx: click.Context,
    operation_id: str,
    dry_run: bool,
    expected_stage: int | None,
    expected_remote_head_sha: str | None,
    reason: str | None,
) -> None:
    """Rebind OPERATION_ID's detached debug stage to its conflict at the published head.

    For a parent repair whose stage is frozen on a commit the parent branch never
    received, so no delegate can be admitted. Apply moves only the stage's
    starting commit and trigger, keeps its deadline, attempts and fence, and
    readies the same delegate. Nothing is pushed or recorded as delivered.
    """
    if not dry_run and not (expected_stage is not None and expected_remote_head_sha and reason):
        raise click.UsageError("--apply requires --stage, --remote-head and --reason")
    args: dict[str, Any] = {"operation_id": operation_id, "dry_run": dry_run}
    if expected_stage is not None:
        args["expected_stage"] = expected_stage
    if expected_remote_head_sha is not None:
        args["expected_remote_head_sha"] = expected_remote_head_sha
    if reason is not None:
        args["reason"] = reason
    _execute(ctx, "integration_rebind_detached_repair", args)


@integration.command("release-delegates")
@click.argument("operation_id")
@click.option("--archive-obsolete", is_flag=True,
              help="Archive obsolete terminal repair stages after checking owners, claims and gates.")
@click.pass_context
@_handle_errors
def integration_release_delegates(
    ctx: click.Context, operation_id: str, archive_obsolete: bool
) -> None:
    """Settle the delegate tasks of an OPERATION_ID that has already ended.

    For one operation that was cancelled or completed before its delegates were
    released. The fleet-wide equivalent is
    `aq doctor --check integration.stranded_delegates --fix`.

    With --archive-obsolete, archive terminal generated repair delegates after
    proving they hold no authority or gates. Earlier stages of a live operation
    may qualify; its current delegate and the operation remain untouched.
    """
    _execute(ctx, "integration_release_delegates", {
        "operation_id": operation_id, "archive_obsolete": archive_obsolete,
    })


@integration.command("recover-candidate-member")
@click.argument("reservation_id")
@click.pass_context
@_handle_errors
def integration_recover_candidate_member(ctx: click.Context, reservation_id: str) -> None:
    """Resolve one pushed frozen candidate-member repair reservation."""
    _execute(ctx, "integration_recover_candidate_member", {"reservation_id": reservation_id})


@integration.command("develop")
@click.argument("project_id")
@click.option("--validation", type=click.Choice(["focused", "advisory", "none"]), default="focused")
@click.option("--command", "commands", multiple=True,
              help="Finite validation preset command; repeatable. No shell or wrapper scripts.")
@click.option("--interval-seconds", type=click.IntRange(min=1), default=300)
@click.option("--timeout-seconds", type=click.IntRange(1, 3600), default=None,
              help="Seconds each command may run (default 300); slot wait is not counted.")
@click.option("--slot-wait-seconds", type=click.IntRange(0, 3600), default=None,
              help="Seconds a command may queue for a test slot before the batch is "
                   "deferred (default 600).")
@click.option("--regenerate", default=None,
              help="Command that rebuilds the files .gitattributes marks merge=aq-generated, "
                   "run after a merge both sides changed one in (e.g. "
                   "scripts/regenerate-generated.sh).")
@click.option("--regenerate-timeout-seconds", type=click.IntRange(1, 3600), default=None,
              help="Seconds one regeneration may run (default 600).")
@click.option("--reason", required=True)
@click.pass_context
@_handle_errors
def integration_develop(
    ctx, project_id, validation, commands, interval_seconds, timeout_seconds,
    slot_wait_seconds, regenerate, regenerate_timeout_seconds, reason,
):
    """Use automatic development batches with explicit local validation."""
    policy = {"validation": validation, "commands": list(commands),
              "interval_seconds": interval_seconds}
    if timeout_seconds is not None:
        policy["timeout_seconds"] = timeout_seconds
    if slot_wait_seconds is not None:
        policy["slot_wait_seconds"] = slot_wait_seconds
    if regenerate is not None:
        policy["regenerate"] = regenerate
    if regenerate_timeout_seconds is not None:
        policy["regenerate_timeout_seconds"] = regenerate_timeout_seconds
    _execute(ctx, "integration_develop", {"project_id": project_id, "reason": reason,
        "policy": policy})


@integration.command("adopt")
@click.argument("project_id")
@click.option("--task", "task_ids", multiple=True, required=True)
@click.option("--target-ref", default="refs/heads/main")
@click.option("--head-sha", required=True)
@click.option("--accept-equivalent", is_flag=True, help="Explicitly accept operator-edited or evidence-only delivery.")
@click.option("--reason", required=True)
@click.pass_context
@_handle_errors
def integration_adopt(ctx, project_id, task_ids, target_ref, head_sha, accept_equivalent, reason):
    """Record already-delivered work without replaying old repair checkpoints."""
    _execute(ctx, "integration_adopt", {"project_id": project_id, "task_ids": list(task_ids),
        "target_ref": target_ref, "head_sha": head_sha, "accept_equivalent": accept_equivalent, "reason": reason})


@integration.command("sweep")
@click.argument("project_id")
@click.option("--retry", is_flag=True, help="Retry parked source revisions.")
@click.option("--recover-child", help="Retry and verify delivery of a completed child task.")
@click.pass_context
@_handle_errors
def integration_development_sweep(ctx, project_id, retry, recover_child):
    """Build and publish a development batch now."""
    _execute(ctx, "integration_development_sweep", {
        "project_id": project_id, "retry": retry, "recover_child": recover_child,
    })


@integration.command("settle-parked")
@click.argument("project_id")
@click.argument("operation_id")
@click.option("--dismiss", is_flag=True,
              help="Only withdraw the park: its sources are merged again on the next sweep "
                   "(and park again if they still conflict). Default: settle them as not owed.")
@click.option("--reason", required=True)
@click.pass_context
@_handle_errors
def integration_settle_parked(ctx, project_id, operation_id, dismiss, reason):
    """Settle a parked development delivery as not owed, or dismiss it.

    OPERATION_ID is a row from `aq integration status PROJECT_ID` `parked`.
    Settling records each parked source (and every repair filed for it) as not
    owed to the row's target: the publisher never merges it there and its
    dependents are released.
    """
    _execute(ctx, "integration_settle_parked", {
        "project_id": project_id, "operation_id": operation_id, "dismiss": dismiss,
        "reason": reason,
    })


@integration.command("migrate-provenance")
@click.argument("project_id")
@click.option("--apply", is_flag=True, help="Publish verified evidence; default is read-only inventory.")
@click.option("--limit", type=click.IntRange(1, 1000), default=500)
@click.option("--offset", type=click.IntRange(min=0), default=0)
@click.option("--task-id", default=None,
              help="Only the sources this held task's close needs; ignores --limit/--offset.")
@click.option("--source", default=None,
              help="With --task-id: attest the exact 40-hex final source of that COMPLETED "
                   "task's current completion, for a legacy close that recorded none.")
@click.pass_context
@_handle_errors
def integration_migrate_provenance(ctx, project_id, apply, limit, offset, task_id, source):
    """Inventory legacy completion generations and exact repair bindings in Git.

    Each page is bounded in time: follow next_offset (and budget_exhausted);
    counts summarises the page.
    """
    if source and not task_id:
        raise click.UsageError("--source attests one task's completion; pass --task-id")
    _execute(ctx, "integration_migrate_provenance", {
        "project_id": project_id, "apply": apply, "limit": limit, "offset": offset,
        **({"task_id": task_id} if task_id else {}),
        **({"source": source} if source else {}),
    })


@integration.command("cancel-preserving")
@click.argument("operation_id")
@click.option("--reason", required=True)
@click.pass_context
@_handle_errors
def integration_cancel_preserving(ctx, operation_id, reason):
    """Cancel obsolete repair scheduling while retaining refs and attached workspaces."""
    _execute(ctx, "integration_cancel_preserving", {"operation_id": operation_id, "reason": reason})


# ---------------------------------------------------------------------------
# aq integration onboard-train
# ---------------------------------------------------------------------------

#: Top-level files that name a project's stack (``train_onboarding.detect_stack``).
_STACK_FILES = (
    "package.json", "package-lock.json", "pnpm-lock.yaml", "pyproject.toml",
    "requirements.txt", ".nvmrc",
)


def _git(repo: str, *args: str) -> str:
    import subprocess

    result = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"git {' '.join(args)} failed in {repo}: {result.stderr.strip() or result.returncode}"
        )
    return result.stdout


def _read_repository(repo: str, ref: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """``(commit, workflows, stack files)`` at ``ref``.  Reads objects only: no checkout."""
    commit = _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()
    listing = _git(repo, "ls-tree", "--name-only", commit, ".github/workflows/").split()
    workflows = {
        path: _git(repo, "show", f"{commit}:{path}")
        for path in listing
        if path.endswith((".yml", ".yaml"))
    }
    top = set(_git(repo, "ls-tree", "--name-only", commit).split())
    files = {name: _git(repo, "show", f"{commit}:{name}") for name in _STACK_FILES if name in top}
    return commit, workflows, files


def _daemon_config() -> dict[str, Any]:
    import os

    import yaml

    path = os.path.expanduser("~/.agent-queue/config.yaml")
    try:
        with open(path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _github_app_id(config: dict[str, Any]) -> int | None:
    integration_config = config.get("integration")
    app = integration_config.get("github_app") if isinstance(integration_config, dict) else None
    if not isinstance(app, dict):
        return None
    try:
        return int(app.get("app_id"))
    except (TypeError, ValueError):
        return None


def _gh_json(*args: str) -> str | None:
    """One best-effort ``gh`` read; ``None`` when gh is absent or refuses."""
    from urllib.parse import urlsplit

    from src.git.github_cli import ExistingLoginCredentials, GhRunner
    from src.git.github_contracts import GitHubAccessError

    pr_view = args[:2] == ("pr", "view")
    if pr_view:
        parsed = urlsplit(args[2])
        parts = parsed.path.strip("/").split("/")
        if parsed.netloc != "github.com" or len(parts) != 4 or parts[2] != "pull":
            return None
        args = ("api", f"repos/{parts[0]}/{parts[1]}/pulls/{parts[3]}", "--jq", ".state")

    async def _read() -> str | None:
        runner = GhRunner(ExistingLoginCredentials(), timeout=20)
        result = await runner.run(args, hostname="github.com", check=False)
        if result.returncode != 0:
            return None
        value = result.stdout.decode("utf-8").strip()
        return value.upper() if pr_view else value

    try:
        return _run(_read())
    except (GitHubAccessError, OSError, UnicodeDecodeError):
        return None


def _render_onboarding(data: dict[str, Any]) -> None:
    from .app import console

    console.print(
        f"[bold]{data['project_id']}[/]: shape [cyan]{data['shape']}[/], "
        f"path [cyan]{data['path']}[/]"
    )
    source = data.get("source") or {}
    if source:
        console.print(f"read {source.get('repo')} at {source.get('ref')} ({source.get('commit')})")
    if data.get("required_checks"):
        console.print(
            f"required checks ({data.get('check_version')}, "
            f"{data.get('credential_mode')} producer):"
        )
        for name in data["required_checks"]:
            console.print(f"  - {name}")
    for report in data.get("workflows") or []:
        gate = "gates the train" if report["push_on_train_refs"] else "does not run on train refs"
        console.print(f"[dim]{report['path']}: {gate}[/]")
        for job in report["jobs"]:
            reason = f" ({job['reason']})" if job.get("reason") else ""
            console.print(f"[dim]  {job['job_id']}: {job['status']}{reason}[/]")
        for note in report.get("notes") or []:
            console.print(f"[yellow]  note: {note}[/]")
    for fix in data.get("trigger_fixes") or []:
        console.print("[bold]Trigger fix[/]")
        console.print(fix, markup=False, highlight=False, soft_wrap=True)
    for command in data.get("validation_commands") or []:
        console.print(f"validation command: {command}", markup=False, soft_wrap=True)
    if data.get("trust_manifest"):
        console.print("App-mode trust manifest: ready (--write-trust-manifest writes it)")
    audit = data.get("audit_workflow") or {}
    if audit and data.get("credential_mode") == "app":
        calls = ", ".join(audit.get("calls") or [])
        if not calls:
            does = "fails an Unattested push job (no gating CI it can call)"
        elif audit.get("fails"):
            does = f"re-runs {calls}; fails an Unattested push job for the rest"
        else:
            does = f"re-runs {calls}"
        console.print(
            f"main push audit: {does} (--write-audit-workflow writes it)", highlight=False
        )
        for item in audit.get("uncalled") or []:
            console.print(
                f"[yellow]  note: not called: {item['path']}: {item['reason']}[/]",
                highlight=False,
            )
    for problem in data.get("problems") or []:
        console.print(f"[red]problem:[/] {problem}", highlight=False)
    for path in data.get("written") or []:
        console.print(f"[green]wrote[/] {path}")
    for number, step in enumerate(data.get("steps") or [], start=1):
        console.print(f"\n[bold]{number}. {step['title']}[/]")
        if step.get("note"):
            console.print(step["note"], highlight=False)
        for command in step.get("commands") or []:
            # Unwrapped, so a long command still pastes as one line.
            console.print(command, markup=False, highlight=False, soft_wrap=True)


@integration.command("onboard-train")
@click.argument("project_id")
@click.option("--repo", "repo_path", type=click.Path(exists=True, file_okay=False),
              help="Checkout to read workflows from (default: the project's workspace).")
@click.option("--ref", help="Ref to read (default: origin/<default branch>). Fetch first if stale.")
@click.option("--repo-url", help="Repository URL when the daemon's project record is unreadable.")
@click.option("--default-branch", help="Default branch when the project record is unreadable.")
@click.option("--credential-mode", type=click.Choice(["auto", "existing-login", "app"]),
              default="auto", show_default=True,
              help="auto reads integration.github_app from ~/.agent-queue/config.yaml.")
@click.option("--route", type=click.Choice(["auto", "shared", "project"]), default="auto",
              show_default=True,
              help="auto: the project's own reviewed pair when shipped, else the shared pair.")
@click.option("--intelligence-class", default="standard-high", show_default=True,
              help="Class hint for primary, verifier and debug repair work; the "
                   "router assigns their profiles.")
@click.option("--check", "checks", multiple=True,
              help="Required check name; repeatable. Replaces the derived set.")
@click.option("--check-version", help="Required-check set version (default: a digest of names).")
@click.option("--github-repository-id", type=click.IntRange(min=1),
              help="Numeric GitHub id for the App-mode trust manifest (default: gh api).")
@click.option("--validation-command", "validation_commands", multiple=True,
              help="Development-path validation command; repeatable. Must be a job preset "
                   "(pytest, aq test, ruff check, npm run build); default: none.")
@click.option("--repository-id",
              help="The project's integration repository id when status does not report it.")
@click.option("--test-command", help="Test command for the --write-workflow template.")
@click.option("--interval-seconds", type=click.IntRange(min=1), default=300, show_default=True)
@click.option("--write-policy", type=click.Path(dir_okay=False),
              help="Write the generated train policy JSON here.")
@click.option("--write-trust-manifest", type=click.Path(dir_okay=False),
              help="Write the App-mode .github/agent-queue-integration.json here.")
@click.option("--write-workflow", type=click.Path(dir_okay=False),
              help="Write a starting CI workflow here (projects without CI).")
@click.option("--write-audit-workflow", type=click.Path(dir_okay=False),
              help="Write the App-mode main push audit (main-attestation.yml) here.")
@click.option("--write-ruleset", type=click.Path(dir_okay=False),
              help="Write the App-mode default-branch ruleset JSON here.")
@click.option("--check-prs/--no-check-prs", default=True, show_default=True,
              help="Ask gh which legacy pull requests are still open.")
@click.pass_context
@_handle_errors
def integration_onboard_train(
    ctx, project_id, repo_path, ref, repo_url, default_branch, credential_mode, route,
    intelligence_class,
    checks, check_version, github_repository_id, validation_commands, repository_id,
    test_command,
    interval_seconds, write_policy, write_trust_manifest, write_workflow,
    write_audit_workflow, write_ruleset, check_prs,
):
    """Plan PROJECT_ID's move onto the integration train; print every step.

    Reads the project from the daemon and its workflows from Git, derives the
    required check-run names, and prints the drain, bind, policy, observe and
    ready-check commands.  Nothing is changed: the mode flips stay with the
    supervisor or local operator.  With --repo and --repo-url it plans from Git
    alone when the daemon's records are out of scope or unreachable.  In App
    credential mode it adds the trust manifest and main push audit, the Actions
    variables and the ruleset, in that order around the drain.  Runbooks:
    docs/config/train-onboarding.md, docs/config/app-mode-train.md.
    """
    import json
    from pathlib import Path

    from src.integration import train_onboarding as onboarding
    from src.playbooks.required import reviewed_bundle_source

    from .exceptions import CommandError, DaemonNotRunningError

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _reads():
        reads: dict[str, Any] = {}
        async with _get_client(api_url) as client:
            for key, command, args in (
                ("project", "get_project", {"project_id": project_id}),
                ("status", "integration_status", {"project_id": project_id}),
                ("completed", "list_tasks", {"project_id": project_id, "status": "COMPLETED"}),
                ("blocked", "list_tasks", {"project_id": project_id, "status": "BLOCKED"}),
            ):
                try:
                    reads[key] = await client.execute(command, args)
                except CommandError:
                    reads[key] = None
        return reads

    try:
        optional = _run(_reads())
    except DaemonNotRunningError:
        if not (repo_path and repo_url):
            raise
        optional = {}
    project = optional.get("project") or {}
    status = optional.get("status") or {}
    repo = repo_path or project.get("workspace")
    if not repo or not Path(repo).is_dir():
        raise click.UsageError(f"{project_id} has no readable workspace; pass --repo PATH")
    notes: list[str] = []
    repository_url = repo_url or project.get("repo_url") or ""
    if not repository_url:
        # The supervisor's grants omit get_project: fall back to the checkout.
        try:
            repository_url = _git(repo, "remote", "get-url", "origin").strip()
        except click.ClickException as exc:
            raise click.UsageError(
                f"cannot read {project_id}'s repository URL; pass --repo-url"
            ) from exc
        notes.append(
            f"repository URL {repository_url!r} read from the checkout's origin; the binding "
            "requires it to match the project record exactly (pass --repo-url to override)"
        )
    branch = default_branch or project.get("repo_default_branch") or "main"
    ref = ref or f"origin/{branch}"
    commit, workflows, files = _read_repository(repo, ref)
    classification = onboarding.classify(repository_url, workflows)

    config = _daemon_config()
    app_id = _github_app_id(config)
    if credential_mode == "auto":
        credential_mode = "app" if app_id is not None else "existing-login"
    full_name = classification.full_name
    if (
        credential_mode == "app"
        and github_repository_id is None
        and full_name
        and classification.shape.startswith("github")
    ):
        found = _gh_json("api", f"repos/{full_name}", "--jq", ".id")
        github_repository_id = int(found) if found and found.isdigit() else None

    parent_route = root_route = None
    if classification.shape.startswith("github"):
        parent_route, root_route = onboarding.select_routes(
            reviewed_bundle_source(), project_id, route
        )

    def _legacy(result: Any) -> list[dict[str, Any]]:
        return [task for task in (result or {}).get("tasks") or [] if isinstance(task, dict)]

    legacy = [
        (task["id"], task["pr_url"])
        for task in _legacy(optional.get("completed"))
        if task.get("pr_url") and not task.get("parent_task_id")
    ]
    unchecked = 0
    if check_prs and legacy:
        # A bounded number of gh reads; the rest are reported, never dropped.
        checked, unchecked = legacy[:50], max(0, len(legacy) - 50)
        legacy = [
            (task_id, url)
            for task_id, url in checked
            if _gh_json("pr", "view", url, "--json", "state", "--jq", ".state") in (None, "OPEN")
        ]
    completed = optional.get("completed") or {}
    if completed.get("hidden_completed") or (completed.get("total") or 0) > len(
        completed.get("tasks") or []
    ):
        notes.append("the daemon capped the completed-task list; some legacy PRs may be unlisted")
    designated = repository_id or status.get("repository_id") or next(
        (
            row.get("repository_id")
            for row in status.get("deliveries") or []
            if isinstance(row, dict) and row.get("repository_id")
        ),
        None,
    )
    facts = onboarding.ProjectFacts(
        project_id=project_id,
        repository_url=repository_url,
        default_branch=branch,
        integration_repository_id=designated,
        current_mode=status.get("effective_mode"),
        legacy_pull_requests=tuple(legacy),
        blocked_tasks=tuple(task["id"] for task in _legacy(optional.get("blocked"))),
        unchecked_pull_requests=unchecked,
    )
    policy_path = write_policy or f"train-policy.{project_id}.json"
    plan = onboarding.plan_onboarding(
        facts,
        classification,
        parent_route=parent_route,
        root_route=root_route,
        credential_mode=credential_mode,
        intelligence_class=intelligence_class,
        check_names=checks or None,
        check_version=check_version,
        attestation_app_id=app_id,
        github_repository_id=github_repository_id,
        validation=validation_commands,
        interval_seconds=interval_seconds,
        policy_path=policy_path,
        manifest_path=write_trust_manifest or onboarding.TRUST_MANIFEST_PATH,
        workflow_path=write_workflow or ".github/workflows/ci.yml",
        audit_workflow_path=write_audit_workflow or onboarding.AUDIT_WORKFLOW_PATH,
        ruleset_path=write_ruleset or f"ruleset.{project_id}.json",
    )

    written: list[str] = []

    def _write(path: str, text: str) -> None:
        Path(path).write_text(text, encoding="utf-8")
        written.append(path)

    if write_policy and plan.policy is not None:
        _write(write_policy, json.dumps(plan.policy, indent=2) + "\n")
    if write_trust_manifest and plan.trust_manifest is not None:
        from src.integration.trust_manifest import canonical_text

        # The same bytes ``aq integration trust-manifest --write`` produces.
        _write(write_trust_manifest, canonical_text(plan.trust_manifest))
    if write_workflow:
        _write(write_workflow, onboarding.ci_workflow_template(files, test_command=test_command))
    if write_audit_workflow:
        if plan.audit_workflow is None:
            notes.append(
                "--write-audit-workflow: only a GitHub project has the main push audit; "
                "nothing written"
            )
        else:
            _write(
                write_audit_workflow,
                onboarding.audit_workflow_template(
                    onboarding.audit_fallback(classification), default_branch=branch
                ),
            )
    if write_ruleset:
        if plan.ruleset is None:
            notes.append(
                "--write-ruleset: the target ruleset needs App credential mode on a GitHub "
                "project and the App id; nothing written"
            )
        else:
            _write(write_ruleset, json.dumps(plan.ruleset, indent=2) + "\n")
    by_path = {report.path: report for report in classification.workflows}
    data = plan.as_dict()
    data["problems"] = [*notes, *data["problems"]]
    data.update(
        source={"repo": repo, "ref": ref, "commit": commit},
        trigger_fixes=[
            onboarding.trigger_fix(by_path[path], workflows[path])
            for path in classification.trigger_fixes
        ],
        written=written,
    )
    emit(ctx, data, entity="integration", render=_render_onboarding)


# ---------------------------------------------------------------------------
# aq integration trust-manifest
# ---------------------------------------------------------------------------


def _trust_manifest_write_command(
    project_id: str, policy_path: str | None, repository_id: str | None
) -> str:
    import shlex

    from src.integration.trust_manifest import TRUST_MANIFEST_PATH

    parts = ["aq", "integration", "trust-manifest", project_id]
    if policy_path:
        parts += ["--policy", policy_path]
    if repository_id:
        parts += ["--repository-id", repository_id]
    parts += ["--write", TRUST_MANIFEST_PATH]
    return shlex.join(parts)


def _check_verdict(committed: dict[str, Any]) -> tuple[bool, list[str]]:
    """``(passes, warning codes)`` for ``--check``: identity decides, the check set warns."""
    passes = bool(committed.get("present") and committed.get("identity_equal"))
    return passes, list(committed.get("warnings") or []) if passes else []


def _render_diff(committed: dict[str, Any], *, indent: str = "  ") -> None:
    import json

    from .app import console

    for item in committed.get("diff") or []:
        have = (
            json.dumps(item.get("committed"))
            if item.get("committed_present", True)
            else "<absent>"
        )
        console.print(
            f"{indent}{item['field']} ({item['kind']}): "
            f"expected {json.dumps(item.get('expected'))}, "
            f"committed {have}",
            markup=False, highlight=False, soft_wrap=True,
        )


def _render_trust_manifest(data: dict[str, Any], *, mode: str, fix: str) -> None:
    from .app import console

    committed = data.get("committed") or {}
    where = (
        f"{committed.get('path')} on {committed.get('ref')}"
        + (f" ({committed['sha']})" if committed.get("sha") else "")
    )
    if mode == "print":
        click.echo(data["text"], nl=False)
        return
    if mode == "write":
        console.print(
            f"[green]wrote[/] {data['written']} (sha256 {data['sha256']}) for "
            f"{data['full_name']} ({data['github_repository_id']}), App "
            f"{data['attestation_app_id']}, {data['policy_source']} policy",
            highlight=False,
        )
    passes, warnings = _check_verdict(committed)
    if not committed.get("present"):
        reason = committed.get("error") or "absent"
        console.print(f"[red]committed copy unavailable[/]: {where}: {reason}", highlight=False)
    elif passes and not warnings:
        console.print(f"[green]committed copy matches[/]: {where}", highlight=False)
    elif passes:
        console.print(f"committed copy matches on identity: {where}", highlight=False)
        for code in warnings:
            console.print(f"[yellow]warning:[/] {code}", highlight=False)
        _render_diff(committed)
    else:
        detail = f": {committed['error']}" if committed.get("error") else ""
        console.print(
            f"[red]committed copy differs[/] ({committed.get('code')}): {where}{detail}",
            highlight=False,
        )
        _render_diff(committed)
    if mode == "check" and not (passes and not warnings):
        console.print("regenerate with:", highlight=False)
        console.print(fix, markup=False, highlight=False, soft_wrap=True)


@integration.command("trust-manifest")
@click.argument("project_id")
@click.option("--policy", "policy_path", type=click.Path(exists=True, dir_okay=False),
              help="Build from this policy JSON instead of the project's bound policy.")
@click.option("--repository-id",
              help="Integration repository id (default: the project's designated one).")
@click.option("--write", "write_path", type=click.Path(dir_okay=False),
              help="Write the canonical manifest to this path.")
@click.option("--check", "check", is_flag=True,
              help="Exit 1 unless the default-branch copy matches on identity.")
@click.option("--print", "print_text", is_flag=True,
              help="Print the canonical manifest text.")
@click.pass_context
@_handle_errors
def integration_trust_manifest(
    ctx: click.Context,
    project_id: str,
    policy_path: str | None,
    repository_id: str | None,
    write_path: str | None,
    check: bool,
    print_text: bool,
) -> None:
    """Render PROJECT_ID's App-mode trust manifest (.github/agent-queue-integration.json).

    The daemon builds it from the policy (--policy FILE, else the bound one),
    the authenticated GitHub binding and its own App, and compares the copy
    committed on the default branch.  --write writes the canonical text,
    --print prints it, and --check exits 1 when the committed copy is missing
    or differs on an identity field; a check-set or formatting difference is a
    warning, because the frozen policy snapshot owns the check set.  Read-only;
    refused under existing-login credentials (not_app_mode).
    """
    import json
    from pathlib import Path

    modes = [name for name, chosen in (
        ("write", write_path is not None), ("check", check), ("print", print_text),
    ) if chosen]
    if len(modes) != 1:
        raise click.UsageError("pass exactly one of --write PATH, --check and --print")
    mode = modes[0]
    args: dict[str, Any] = {"project_id": project_id}
    if policy_path:
        try:
            args["policy"] = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise click.UsageError(f"cannot read the policy {policy_path}: {exc}") from exc
    if repository_id:
        args["repository_id"] = repository_id

    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _request():
        async with _get_client(api_url) as client:
            return await client.execute("integration_trust_manifest", args)

    data = dict(_run(_request()))
    if mode == "write":
        Path(write_path).write_text(data["text"], encoding="utf-8")
        data["written"] = write_path
    fix = _trust_manifest_write_command(project_id, policy_path, repository_id)
    data["write_command"] = fix
    emit(ctx, data, render=lambda value: _render_trust_manifest(value, mode=mode, fix=fix))
    if mode == "check" and not _check_verdict(data.get("committed") or {})[0]:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# aq integration app-verify / app-setup
# ---------------------------------------------------------------------------

_STATUS_STYLE = {"ok": "green", "warn": "yellow", "fail": "red"}


def _app_mode_args(
    project_id: str, policy_path: str | None, repository_id: str | None
) -> dict[str, Any]:
    import json
    from pathlib import Path

    args: dict[str, Any] = {"project_id": project_id}
    if policy_path:
        try:
            args["policy"] = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise click.UsageError(f"cannot read the policy {policy_path}: {exc}") from exc
        args["policy_path"] = policy_path
    if repository_id:
        args["repository_id"] = repository_id
    return args


def _app_verify(ctx: click.Context, args: dict[str, Any]) -> dict[str, Any]:
    api_url = ctx.obj.get("api_url") if ctx.obj else None

    async def _request():
        async with _get_client(api_url) as client:
            return await client.execute("integration_app_verify", args)

    return dict(_run(_request()))


def _app_item(data: dict[str, Any], item_id: str) -> dict[str, Any]:
    return next((item for item in data.get("items") or [] if item.get("id") == item_id), {})


def _compact(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(", ", ": "))


def _render_app_item(item: dict[str, Any], *, detail: bool = True) -> None:
    from .app import console

    status = item.get("status") or "?"
    style = _STATUS_STYLE.get(status, "white")
    codes = ", ".join(item.get("codes") or [])
    observed = item.get("observed")
    if item.get("id") == "protection" and isinstance(observed, dict):
        # The classification is what runbook §9.3 checks, even when ok.
        classification = observed.get("classification")
        if classification:
            codes = f"{codes} ({classification})" if codes else str(classification)
    console.print(
        f"  [{style}]{status:<4}[/] {item.get('id', '?'):<15} {codes}".rstrip(),
        highlight=False,
    )
    if not detail or status == "ok":
        return
    if item.get("id") == "manifest" and isinstance(observed, dict):
        if observed.get("error"):
            console.print(f"       {observed['error']}", markup=False, highlight=False)
        _render_diff(observed, indent="       ")
    else:
        if item.get("expected") is not None:
            console.print(
                f"       expected: {_compact(item['expected'])}",
                markup=False, highlight=False, soft_wrap=True,
            )
        if observed is not None:
            console.print(
                f"       observed: {_compact(observed)}",
                markup=False, highlight=False, soft_wrap=True,
            )
    if item.get("fix"):
        console.print(f"       fix: {item['fix']}", markup=False, highlight=False, soft_wrap=True)


def _render_app_verify(data: dict[str, Any]) -> None:
    from .app import console

    console.print(
        f"App mode for {data.get('project_id')}: {data.get('repository_id')} "
        f"({data.get('full_name')} {data.get('github_repository_id')}), "
        f"App {data.get('attestation_app_id')}, {data.get('policy_source')} policy",
        highlight=False,
    )
    for item in data.get("items") or []:
        _render_app_item(item)
    blockers, warnings = data.get("blockers") or [], data.get("warnings") or []
    verdict = "[green]ready[/]" if data.get("ready") else "[red]not ready[/]"
    console.print(
        f"{verdict}: {len(blockers)} blocker(s), {len(warnings)} warning(s)", highlight=False
    )


@integration.command("app-verify")
@click.argument("project_id")
@click.option("--policy", "policy_path", type=click.Path(exists=True, dir_okay=False),
              help="Verify against this policy JSON instead of the project's bound policy.")
@click.option("--repository-id",
              help="Integration repository id (default: the project's designated one).")
@click.pass_context
@_handle_errors
def integration_app_verify(
    ctx: click.Context, project_id: str, policy_path: str | None, repository_id: str | None
) -> None:
    """Check everything PROJECT_ID's App credential mode depends on.

    One item per concern -- credential, repository, producer, manifest,
    variables, protection, audit_workflow -- each ok, warn or fail with a
    code, what was expected, what GitHub showed and the exact fix.  The same
    items drive the functional preflight: every fail is a blocker of the same
    name and every warn a status warning.  Read-only; exits 1 when an item
    fails.  --json carries expected.ruleset, the target ruleset JSON.
    """
    data = _app_verify(ctx, _app_mode_args(project_id, policy_path, repository_id))
    emit(ctx, data, render=_render_app_verify)
    if not data.get("ready"):
        raise SystemExit(1)


def _gh_variable_set(name: str, full_name: str, value: str) -> list[str]:
    return ["gh", "variable", "set", name, "--repo", full_name, "--body", value]


def _differing_variables(item: dict[str, Any]) -> list[str] | None:
    """Names whose value differs from the expected one; ``None`` when unread."""
    expected, observed = item.get("expected"), item.get("observed")
    values = observed.get("values") if isinstance(observed, dict) else None
    if not isinstance(expected, dict) or not isinstance(values, dict):
        return None
    return [
        name for name, value in expected.items()
        if value is not None and values.get(name) != value
    ]


def _run_gh(argv: list[str]) -> dict[str, Any]:
    import subprocess

    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": None, "error": f"{type(exc).__name__}: {exc}"}
    outcome: dict[str, Any] = {"returncode": result.returncode}
    if result.returncode != 0:
        outcome["error"] = result.stderr.strip() or f"exit {result.returncode}"
    return outcome


#: Protection codes the §8.1 target ruleset resolves; any other code keeps its own fix.
_RULESET_FIXES = frozenset({
    "main_protection_missing", "branch_protection_incompatible", "main_protection_app_bypass",
})


def _ruleset_commands(full_name: str, ruleset_id: Any) -> list[str]:
    put = f"gh api --method PUT repos/{full_name}/rulesets/{ruleset_id or 'RULESET_ID'} --input FILE"
    post = f"gh api --method POST repos/{full_name}/rulesets --input FILE"
    return [put] if ruleset_id else [put, post]


def _render_app_setup(data: dict[str, Any]) -> None:
    import json

    from .app import console

    verify = data["verify"]
    console.print(
        f"App-mode setup for {verify.get('project_id')}: {verify.get('full_name')} "
        f"({verify.get('github_repository_id')}), App {verify.get('attestation_app_id')}",
        highlight=False,
    )

    variables = data["variables"]
    console.print("\n[bold]variables[/]", highlight=False)
    _render_app_item(variables["item"], detail=False)
    if variables["differing"] is None:
        console.print(
            "  not checked; fix the credential first", markup=False, highlight=False
        )
    elif not variables["differing"]:
        console.print("  both variables are already correct; nothing to set", highlight=False)
    elif variables["applied"] is None:
        console.print("  set the differing variables (or rerun with --apply):", highlight=False)
        for command in variables["commands"]:
            console.print(f"  {command}", markup=False, highlight=False, soft_wrap=True)
    else:
        for applied in variables["applied"]:
            result = "set" if applied.get("returncode") == 0 else f"failed: {applied.get('error')}"
            console.print(f"  {applied['name']}: {result}", markup=False, highlight=False)
        console.print("  re-verified:", highlight=False)
        _render_app_item(variables["reverified"] or {})

    manifest = data["manifest"]
    console.print("\n[bold]manifest[/]", highlight=False)
    _render_app_item(manifest["item"], detail=False)
    if manifest["item"].get("status") != "ok":
        console.print(f"  {manifest['command']}", markup=False, highlight=False, soft_wrap=True)
        console.print(
            f"  then commit {manifest['path']} to {manifest['ref']} of {manifest['full_name']} "
            "through the project's current delivery path (runbook §9.2 step 3)",
            markup=False, highlight=False, soft_wrap=True,
        )

    protection = data["protection"]
    console.print("\n[bold]protection[/]", highlight=False)
    _render_app_item(protection["item"], detail=False)
    codes = set(protection["item"].get("codes") or ())
    if codes & _RULESET_FIXES and protection["ruleset"] is not None:
        console.print(
            "  the target ruleset (save it as FILE; applying it is the repository admin's step, "
            "runbook §9.3 step 4):",
            highlight=False,
        )
        click.echo(json.dumps(protection["ruleset"], indent=2))
        for command in protection["commands"]:
            console.print(f"  {command}", markup=False, highlight=False, soft_wrap=True)
    elif protection["item"].get("status") != "ok" and protection["item"].get("fix"):
        # Unreadable, or a development project the target ruleset would keep
        # blocked: the item's own fix, never a ruleset write.
        console.print(
            f"  {protection['item']['fix']}", markup=False, highlight=False, soft_wrap=True
        )

    others = [
        item for item in verify.get("items") or []
        if item.get("id") not in {"variables", "manifest", "protection"}
        and item.get("status") != "ok"
    ]
    if others:
        console.print("\n[bold]other items[/]", highlight=False)
        for item in others:
            _render_app_item(item)


@integration.command("app-setup")
@click.argument("project_id")
@click.option("--policy", "policy_path", type=click.Path(exists=True, dir_okay=False),
              help="Set up against this policy JSON instead of the project's bound policy.")
@click.option("--repository-id",
              help="Integration repository id (default: the project's designated one).")
@click.option("--apply", "apply", is_flag=True,
              help="Run gh variable set for every variable that differs, then verify again.")
@click.pass_context
@_handle_errors
def integration_app_setup(
    ctx: click.Context,
    project_id: str,
    policy_path: str | None,
    repository_id: str | None,
    apply: bool,
) -> None:
    """Print, per App-mode concern, what is wrong and the exact fix.

    Runs app-verify, then: for the two Actions variables prints the
    `gh variable set` commands, and with --apply runs them with your own gh
    login (a repository admin) for exactly the variables that differ, then
    verifies again and prints the variables item; a variable that is already
    correct is never touched.  For the trust manifest it prints the
    trust-manifest --write command and where to commit the file; for
    protection the target ruleset JSON and the gh api command.  It never
    writes repository files and never applies a ruleset.
    """
    args = _app_mode_args(project_id, policy_path, repository_id)
    verify = _app_verify(ctx, args)
    full_name = verify.get("full_name")
    expected = verify.get("expected") or {}

    variables_item = _app_item(verify, "variables")
    differing = _differing_variables(variables_item)
    wanted = variables_item.get("expected") or {}
    commands = [
        _gh_variable_set(name, full_name, wanted[name]) for name in differing or []
    ]
    applied = reverified = None
    if apply and commands:
        applied = [
            {"name": argv[3], "command": _shlex_join(argv), **_run_gh(argv)} for argv in commands
        ]
        reverified = _app_item(_app_verify(ctx, args), "variables")

    manifest_item = _app_item(verify, "manifest")
    protection_item = _app_item(verify, "protection")
    observed_protection = protection_item.get("observed")
    ruleset_id = (
        observed_protection.get("ruleset_id") if isinstance(observed_protection, dict) else None
    )
    data = {
        "verify": verify,
        "variables": {
            "item": variables_item,
            "differing": differing,
            "commands": [_shlex_join(argv) for argv in commands],
            "applied": applied,
            "reverified": reverified,
        },
        "manifest": {
            "item": manifest_item,
            "command": _trust_manifest_write_command(project_id, policy_path, repository_id),
            "path": expected.get("manifest_path"),
            "ref": verify.get("default_branch"),
            "full_name": full_name,
        },
        "protection": {
            "item": protection_item,
            "ruleset": expected.get("ruleset"),
            "commands": _ruleset_commands(full_name, ruleset_id),
        },
    }
    emit(ctx, data, render=_render_app_setup)
    if applied is not None and (
        any(item.get("returncode") != 0 for item in applied)
        or (reverified or {}).get("status") != "ok"
    ):
        raise SystemExit(1)


def _shlex_join(argv: list[str]) -> str:
    import shlex

    return shlex.join(argv)
