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
    """Inspect integration Subjects and apply current recovery proofs."""


def _cutover(ctx, command, project_id, flow_file, reverse, allow_epics,
             apply=False, expected_generation=None, plan_file=None):
    import json
    from pathlib import Path

    import yaml

    if not reverse and flow_file is None:
        raise click.UsageError("forward cutover requires --flow FILE")
    if reverse and flow_file is not None:
        raise click.UsageError("--reverse restores the prior flow; omit --flow")
    if apply and expected_generation is None:
        raise click.UsageError("--apply requires --expected-generation from cutover-plan")
    if apply and plan_file is None:
        raise click.UsageError("--apply requires --plan FILE saved before manual GitHub changes")
    args = {"project_id": project_id, "reverse": reverse, "allow_epics": allow_epics,
            "dry_run": not apply, "expected_generation": expected_generation}
    if flow_file is not None:
        try:
            args["flow"] = yaml.safe_load(Path(flow_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise click.UsageError(f"cannot read flow: {exc}") from exc
    if plan_file is not None:
        try:
            document = json.loads(Path(plan_file).read_text(encoding="utf-8"))
            document = document.get("data", document)
            document = document.get("value", document)
            args["baseline"] = document.get("plan", document)
        except (OSError, UnicodeError, ValueError, AttributeError) as exc:
            raise click.UsageError(f"cannot read saved plan: {exc}") from exc

    async def run():
        async with _get_client(ctx.obj.get("api_url") if ctx.obj else None) as client:
            return await client.execute(command, args)

    emit(ctx, _run(run()), render=lambda value: click.echo(json.dumps(value, indent=2)))


@integration.command("cutover-plan")
@click.option("--project", "project_id", envvar="AQ_PROJECT_ID", required=True)
@click.option("--flow", "flow_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--reverse", is_flag=True, help="Plan restoration of the last cutover.")
@click.option("--allow-epics", is_flag=True, help="Permit live aq/epic targets at the barrier.")
@click.pass_context
@_handle_errors
def integration_cutover_plan(ctx, project_id, flow_file, reverse, allow_epics):
    """Print ordered changes, live authority and PRs without writing state."""
    _cutover(ctx, "integration_cutover_plan", project_id, flow_file, reverse, allow_epics)


@integration.command("cutover")
@click.option("--project", "project_id", envvar="AQ_PROJECT_ID", required=True)
@click.option("--flow", "flow_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--reverse", is_flag=True, help="Restore the prior binding, flow and data fixes.")
@click.option("--allow-epics", is_flag=True, help="Keep live aq/epic targets intact.")
@click.option("--apply/--dry-run", default=False, help="Apply the plan; default is preview.")
@click.option("--expected-generation", type=click.IntRange(min=0))
@click.option("--plan", "plan_file", type=click.Path(exists=True, dir_okay=False),
              help="Saved cutover-plan JSON from before manual GitHub changes.")
@click.pass_context
@_handle_errors
def integration_cutover(ctx, project_id, flow_file, reverse, allow_epics, apply,
                        expected_generation, plan_file):
    """Preview or atomically rebind the train after its quiescence barrier."""
    _cutover(ctx, "integration_cutover", project_id, flow_file, reverse, allow_epics,
             apply, expected_generation, plan_file)


@integration.command("pause-batch")
@click.argument("batch_id")
@click.option("--reason", default="", help="Explanation recorded with the intent.")
@click.option("--apply/--dry-run", default=False, help="Pause publication; default is preview.")
@click.pass_context
@_handle_errors
def integration_pause_batch(ctx, batch_id, reason, apply):
    """Preview or pause an unpromoted Git-first BATCH_ID."""
    _execute(ctx, "integration_pause_batch", {
        "batch_id": batch_id, "reason": reason, "dry_run": not apply,
    })


@integration.command("resume-batch")
@click.argument("batch_id")
@click.option("--reason", default="", help="Explanation recorded with the intent.")
@click.option("--apply/--dry-run", default=False, help="Resume publication; default is preview.")
@click.pass_context
@_handle_errors
def integration_resume_batch(ctx, batch_id, reason, apply):
    """Preview or resume a paused Git-first BATCH_ID."""
    _execute(ctx, "integration_resume_batch", {
        "batch_id": batch_id, "reason": reason, "dry_run": not apply,
    })


@integration.command("eject")
@click.option("--batch", "batch_id", required=True, help="Frozen Git-first batch id.")
@click.option("--task", "task_id", required=True, help="Member to return to pending.")
@click.option("--reason", default="", help="Required explanation when ejecting.")
@click.option("--apply/--dry-run", default=False, help="Replace the batch; default is preview.")
@click.pass_context
@_handle_errors
def integration_eject(ctx, batch_id, task_id, reason, apply):
    """Abort a batch and freeze its remainder without changing PR approvals."""
    if apply and not reason.strip():
        raise click.UsageError("--apply needs a nonblank --reason")
    _execute(ctx, "integration_eject", {
        "batch_id": batch_id, "task_id": task_id, "reason": reason, "dry_run": not apply,
    })


@integration.command("seal-now")
@click.option("--project", "project_id", required=True, help="Project whose root target to seal.")
@click.option("--apply/--dry-run", default=False, help="Bypass cadence once; default is preview.")
@click.pass_context
@_handle_errors
def integration_seal_now(ctx, project_id, apply):
    """Freeze currently eligible root inputs immediately."""
    _execute(ctx, "integration_seal_now", {"project_id": project_id, "dry_run": not apply})


@integration.command("record-root-noop")
@click.argument("task_id")
@click.option("--apply/--dry-run", default=False, help="Record the completion; default is preview.")
@click.option("--head", "expected_head_sha", help="Exact published head returned by preview.")
@click.option("--reason", default="", help="Why the root produced no artifact.")
@click.pass_context
@_handle_errors
def integration_record_root_noop(ctx, task_id, apply, expected_head_sha, reason):
    """Verify and complete an unheld no-code root, retaining its Git evidence."""
    if apply and (not expected_head_sha or not reason.strip()):
        raise click.UsageError("--apply requires --head from preview and a nonblank --reason")
    args = {"task_id": task_id, "dry_run": not apply, "reason": reason}
    if expected_head_sha:
        args["expected_head_sha"] = expected_head_sha
    _execute(ctx, "integration_record_root_noop", args)


@integration.command("abort-batch")
@click.argument("batch_id")
@click.option("--reason", default="", help="Required explanation when aborting.")
@click.option("--apply", is_flag=True, help="Abort the batch; default is preview.")
@click.pass_context
@_handle_errors
def integration_abort_batch(ctx, batch_id, reason, apply):
    """Preview or abort an unpromoted Git-first BATCH_ID."""
    if apply and not reason.strip():
        raise click.UsageError("--apply needs a nonblank --reason")
    _execute(ctx, "integration_abort_batch", {
        "batch_id": batch_id, "reason": reason, "dry_run": not apply,
    })


@integration.command("refresh-epic")
@click.option("--task", "task_id", required=True, help="Epic whose branch needs the default branch.")
@click.option("--apply", is_flag=True, help="Start or advance the attested refresh; default is preview.")
@click.pass_context
@_handle_errors
def integration_refresh_epic(ctx, task_id, apply):
    """Preview or refresh an epic; pending checks/repairs continue through the train."""
    _execute(ctx, "integration_refresh_epic", {"task_id": task_id, "dry_run": not apply})


@integration.command("retire-origin")
@click.argument("task_id")
@click.option("--origin-id", default=None, help="Exact origin id returned by the preview.")
@click.option("--reason", default="", help="Required explanation when retiring.")
@click.option("--apply", is_flag=True, help="Retire the delivered origin; default is preview.")
@click.pass_context
@_handle_errors
def integration_retire_origin(ctx, task_id, origin_id, reason, apply):
    """Preview or retire a delivered TASK_ID's branch origin."""
    if apply and (not origin_id or not reason.strip()):
        raise click.UsageError("--apply needs --origin-id and a nonblank --reason")
    _execute(ctx, "integration_retire_origin", {
        "task_id": task_id, "origin_id": origin_id, "reason": reason, "dry_run": not apply,
    })


@integration.command("release-held-gate")
@click.argument("subject_id")
@click.argument("gate_id")
@click.option("--expected-version", type=click.IntRange(min=0), default=None,
              help="Exact subject version returned by the preview.")
@click.option("--reason", default="", help="Required explanation when releasing the hold.")
@click.option("--apply", is_flag=True, help="Release the hold; default is preview.")
@click.pass_context
@_handle_errors
def integration_release_held_gate(ctx, subject_id, gate_id, expected_version, reason, apply):
    """Preview or release a human hold on a parent SUBJECT_ID and GATE_ID."""
    if apply and (expected_version is None or not reason.strip()):
        raise click.UsageError("--apply needs --expected-version and a nonblank --reason")
    _execute(ctx, "integration_release_held_gate", {
        "subject_id": subject_id, "gate_id": gate_id, "expected_version": expected_version,
        "reason": reason, "dry_run": not apply,
    })


def _expected_versions(items: tuple[str, ...]) -> dict[str, int]:
    """Parse repeated ``SUBJECT_ID:VERSION`` fences into exact subject versions."""
    versions: dict[str, int] = {}
    for item in items:
        try:
            subject_id, version = item.rsplit(":", 1)
            if not subject_id or int(version) < 0 or subject_id in versions:
                raise ValueError
            versions[subject_id] = int(version)
        except ValueError:
            raise click.BadParameter("use unique SUBJECT_ID:VERSION with a nonnegative version",
                                     param_hint="--expected-subject") from None
    return versions


def _require_transfer_apply_fences(engine: str, versions: dict[str, int], reason: str,
                                   evidence: tuple[str, ...]) -> None:
    """An apply needs the whole previewed subject set, a reason and evidence."""
    if not versions or not reason.strip() or (engine == "reconciler" and not evidence):
        raise click.UsageError("--apply needs exact subject versions, a reason and cutover evidence")


@integration.command("engine-transfer")
@click.argument("repository_id")
@click.option("--engine", type=click.Choice(["reconciler"]), required=True)
@click.option("--parent-task-id", default=None,
              help="Transfer this parent instead of repository roots.")
@click.option("--expected-subject", "expected_subjects", multiple=True,
              help="Exact SUBJECT_ID:VERSION from the preview; repeat for every selected subject.")
@click.option("--reason", default="", help="Required explanation when applying the transfer.")
@click.option("--evidence", multiple=True, help="Shadow, scenario and operator approval references.")
@click.option("--apply", is_flag=True, help="Apply the exact previewed versions; default is preview.")
@click.pass_context
@_handle_errors
def integration_engine_transfer(
    ctx, repository_id, engine, parent_task_id, expected_subjects, reason, evidence, apply,
):
    """Preview or transfer exclusive integration ownership for REPOSITORY_ID."""
    versions = _expected_versions(expected_subjects)
    if apply:
        _require_transfer_apply_fences(engine, versions, reason, evidence)
    _execute(ctx, "integration_engine_transfer", {
        "repository_id": repository_id, "engine": engine, "expected_versions": versions,
        "reason": reason, "evidence": list(evidence), "dry_run": not apply,
        **({"parent_task_id": parent_task_id} if parent_task_id else {}),
    })


@integration.command("development-engine-transfer")
@click.argument("project_id")
@click.option("--engine", type=click.Choice(["reconciler"]), required=True)
@click.option("--expected-subject", "expected_subjects", multiple=True,
              help="Exact SUBJECT_ID:VERSION from the preview; repeat for every Development subject.")
@click.option("--reason", default="", help="Required explanation when applying the transfer.")
@click.option("--evidence", multiple=True,
              help="Shadow, scenario and operator approval references.")
@click.option("--apply", is_flag=True,
              help="Apply the exact previewed versions; default is preview.")
@click.pass_context
@_handle_errors
def integration_development_engine_transfer(
    ctx, project_id, engine, expected_subjects, reason, evidence, apply,
):
    """Preview or transfer Development ownership for every subject of PROJECT_ID.

    The apply activates reconciler ownership of the whole project: it runs inside
    the repository engine fence a publisher shares, needs the exact versions
    the preview printed, and refuses while a publish write is unconfirmed in
    either direction.
    """
    versions = _expected_versions(expected_subjects)
    if apply:
        _require_transfer_apply_fences(engine, versions, reason, evidence)
    _execute(ctx, "integration_development_engine_transfer", {
        "project_id": project_id, "engine": engine, "expected_versions": versions,
        "reason": reason, "evidence": list(evidence), "dry_run": not apply,
    })


@integration.command("status")
@click.argument("project_id")
@click.option("--control-only", is_flag=True, help="Read durable control state without readiness observations.")
@click.pass_context
@_handle_errors
def integration_status(ctx: click.Context, project_id: str, control_only: bool) -> None:
    """Show current Subjects, configuration and Git delivery for PROJECT_ID."""
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




@integration.command("reevaluate-repair")
@click.argument("operation_id")
@click.option("--apply", is_flag=True, help="Apply the exact preview; default is read-only.")
@click.option("--head", default=None)
@click.option("--episode", default=None)
@click.option("--generation", type=int, default=None)
@click.option("--stage", type=int, default=None)
@click.option("--fence", type=int, default=None)
@click.option("--snapshot", default=None)
@click.option("--reason", default=None)
@click.pass_context
@_handle_errors
def integration_reevaluate_repair(ctx, operation_id, apply, head, episode, generation, stage, fence, snapshot, reason):
    """Preview settlement of an audited no-op repair with trusted exact-head green CI."""
    if apply and (not head or generation is None or stage is None or fence is None
                  or not snapshot or not (reason or "").strip()):
        raise click.UsageError("--apply requires --head, --generation, --stage, --fence, --snapshot and --reason")
    args = {"operation_id": operation_id, "dry_run": not apply}
    for key, value in (("expected_head_sha", head), ("expected_episode_id", episode),
                       ("expected_generation", generation), ("expected_stage", stage),
                       ("expected_fence_token", fence), ("reason", reason)):
        if value is not None:
            args[key] = value
    if snapshot is not None:
        args["expected_snapshot_digest"] = snapshot
    _execute(ctx, "integration_reevaluate_repair", args)




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
    An escalated no-progress stage can resume a later child conflict after
    proving its detached collector, published head and terminal stage history.
    A conflict already bound to an exhausted stage cannot buy another budget.
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
@click.option("--head", "head_sha", required=True, help="Exact published aggregate commit SHA.")
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
    """Prove a completed parent repair and any subsequent child collection.

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


@integration.command("recover-candidate-member")
@click.argument("reservation_id")
@click.pass_context
@_handle_errors
def integration_recover_candidate_member(ctx: click.Context, reservation_id: str) -> None:
    """Resolve one pushed frozen candidate-member repair reservation."""
    _execute(ctx, "integration_recover_candidate_member", {"reservation_id": reservation_id})


# ---------------------------------------------------------------------------
# App-mode diagnostics and trust manifests
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
@click.option("--repository-access-only", is_flag=True,
              help="Verify repository access before integration is configured.")
@click.pass_context
@_handle_errors
def integration_app_verify(
    ctx: click.Context, project_id: str, policy_path: str | None, repository_id: str | None,
    repository_access_only: bool = False,
) -> None:
    """Check everything PROJECT_ID's App credential mode depends on.

    One item per concern -- credential, repository, producer, manifest,
    variables, protection, audit_workflow -- each ok, warn or fail with a
    code, what was expected, what GitHub showed and the exact fix.  The same
    items drive the functional preflight: every fail is a blocker of the same
    name and every warn a status warning.  Read-only; exits 1 when an item
    fails.  --json carries expected.ruleset, the target ruleset JSON.
    """
    args = _app_mode_args(project_id, policy_path, repository_id)
    if repository_access_only:
        args["repository_access_only"] = True
    data = _app_verify(ctx, args)
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

_STATUS_STYLE = {"ok": "green", "warn": "yellow", "fail": "red"}

def _compact(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, separators=(", ", ": "))

_RULESET_FIXES = frozenset({
    "main_protection_missing", "branch_protection_incompatible", "main_protection_app_bypass",
})
