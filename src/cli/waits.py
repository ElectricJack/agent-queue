"""Register a typed wait, then read its durable result pointer."""

from __future__ import annotations

import click
import json
from pathlib import Path

from .app import _get_client, _handle_errors, _run, cli
from .claim_epoch import claim_epoch_option, resolve_claim_epoch
from .envelope import emit


def _execute(ctx, command, params):
    async def run():
        async with _get_client((ctx.obj or {}).get("api_url")) as client:
            return await client.execute(command, params)

    return _run(run())


@cli.group("wait")
def wait():
    """Register verified conditions and inspect durable wait evidence."""


@wait.command("register")
@click.option(
    "--kind",
    type=click.Choice(
        [
            "job",
            "task",
            "message",
            "timer",
            "dependency",
            "ci_check",
            "ci_run",
            "pull_request",
            "batch",
            "human",
            "resource",
            "playbook_condition",
        ]
    ),
    default=None,
)
@click.option("--schema-version", type=click.Choice(["1", "2"]), default=None)
@click.option(
    "--condition", default=None, help="Typed condition JSON, or @file; selects version 2."
)
@click.option("--task", "producer_task", default=None, help="Producer task ID; selects version 2.")
@click.option("--sha", default=None, help="Exact CI/PR head SHA; selects version 2.")
@click.option("--repository", default=None)
@click.option("--check-name", default=None)
@click.option("--run-id", default=None)
@click.option("--pr-number", type=click.IntRange(min=1), default=None)
@click.option("--claimed", default=None, help="Commentary, kept separate from verified evidence.")
@click.option("--escalate-after-seconds", type=click.FloatRange(min=0, min_open=True, max=86400))
@click.option(
    "--ref",
    default=None,
    help=(
        "Job ID, task ID or message thread ID (not a message ID). For a reply, send with "
        "aq message send --thread-id ID --from-kind session --from-id $AQ_SESSION_ID; "
        "use that ID here."
    ),
)
@click.option(
    "--after-seq",
    type=click.IntRange(min=0),
    default=None,
    help="Required for message waits; use message.created_seq from aq message send --json.",
)
@click.option("--due-at", type=float, default=None, help="Timer due instant in UTC epoch seconds.")
@click.option("--timeout", type=click.FloatRange(min=0, min_open=True, max=86400), default=None)
@click.option("--idempotency-key", required=True)
@click.option("--project-id", default=None)
@claim_epoch_option
@click.pass_context
@_handle_errors
def wait_register(
    ctx,
    kind,
    ref,
    after_seq,
    due_at,
    timeout,
    idempotency_key,
    project_id,
    claim_epoch,
    schema_version,
    condition,
    producer_task,
    sha,
    repository,
    check_name,
    claimed,
    escalate_after_seconds,
    run_id,
    pr_number,
):
    """Register one condition and end the turn until its result pointer arrives."""
    if not kind and not condition and not producer_task:
        raise click.UsageError("--kind or --condition is required")
    version = (
        int(schema_version)
        if schema_version
        else 2
        if (
            condition
            or producer_task
            or sha
            or claimed is not None
            or escalate_after_seconds is not None
            or kind not in {"task", "job", "message", "timer"}
        )
        else 1
    )
    if version == 1 and any(
        value is not None
        for value in (
            condition,
            producer_task,
            sha,
            repository,
            check_name,
            run_id,
            pr_number,
            claimed,
            escalate_after_seconds,
        )
    ):
        raise click.UsageError("version 2 options require --schema-version 2")
    if kind == "message" and after_seq is None and not condition:
        raise click.UsageError(
            "--after-seq is required for --kind message; use message.created_seq "
            "from aq message send --json"
        )
    params = dict(
        kind=kind,
        ref=ref,
        after_seq=after_seq,
        due_at=due_at,
        timeout=timeout,
        idempotency_key=idempotency_key,
        project_id=project_id,
        claim_epoch=resolve_claim_epoch(claim_epoch),
    )
    if version == 2:
        if condition:
            if any(
                value is not None
                for value in (
                    producer_task,
                    ref,
                    after_seq,
                    due_at,
                    sha,
                    repository,
                    check_name,
                    run_id,
                    pr_number,
                )
            ):
                raise click.UsageError("--condition cannot be mixed with source shortcuts")
            try:
                raw = Path(condition[1:]).read_text() if condition.startswith("@") else condition
                typed = json.loads(raw)
            except (OSError, ValueError) as exc:
                raise click.UsageError(f"invalid condition JSON: {exc}") from exc
            if not isinstance(typed, dict):
                raise click.UsageError("--condition must be a JSON object")
        else:
            typed = {"kind": kind or "task"}
            if producer_task or (kind == "task" and ref):
                typed["task_id"] = producer_task or ref
            elif kind in {"job", "batch"}:
                typed[f"{kind}_id"] = ref
            elif kind == "message":
                typed.update(thread_id=ref, after_seq=after_seq)
            elif kind == "timer":
                typed["due_at"] = due_at
            if sha:
                typed["expected_head_sha" if kind == "pull_request" else "head_sha"] = sha
            if repository:
                typed["repository_id"] = repository
            if check_name:
                typed["check_name"] = check_name
            if run_id:
                typed["run_id"] = run_id
            if pr_number:
                typed["pr_number"] = pr_number
        for key in ("ref", "after_seq", "due_at"):
            params.pop(key)
        params.update(
            schema_version=2,
            condition=typed,
            claimed=claimed,
            escalate_after_seconds=escalate_after_seconds,
        )
    elif schema_version:
        params["schema_version"] = 1
    emit(ctx, _execute(ctx, "wait_register", {k: v for k, v in params.items() if v is not None}))


@wait.command("show")
@click.argument("wait_id")
@click.option("--consume", is_flag=True, help="Consume only this terminal result's notification.")
@click.option("--schema-version", type=click.Choice(["1", "2"]), default=None)
@claim_epoch_option
@click.pass_context
@_handle_errors
def wait_show(ctx, wait_id, consume, claim_epoch, schema_version):
    """Read the wait's state, bounded digest and result reference."""
    params = {"wait_id": wait_id}
    if schema_version:
        params["schema_version"] = int(schema_version)
    if consume:
        params["consume"] = True
        epoch = resolve_claim_epoch(claim_epoch)
        if epoch is not None:
            params["claim_epoch"] = epoch
    emit(ctx, _execute(ctx, "wait_get", params))


@wait.command("list")
@click.option("--project-id", default=None)
@click.option("--limit", type=click.IntRange(1, 100), default=100)
@click.option("--offset", type=click.IntRange(min=0), default=0)
@click.option("--schema-version", type=click.Choice(["1", "2"]), default=None)
@click.pass_context
@_handle_errors
def wait_list(ctx, project_id, limit, offset, schema_version):
    """Read current task history, including waits from earlier claims."""
    params = {"limit": limit, "offset": offset}
    if project_id is not None:
        params["project_id"] = project_id
    if schema_version:
        params["schema_version"] = int(schema_version)
    emit(ctx, _execute(ctx, "wait_list", params))


@wait.command("cancel")
@click.argument("wait_id")
@click.option("--schema-version", type=click.Choice(["1", "2"]), default=None)
@claim_epoch_option
@click.pass_context
@_handle_errors
def wait_cancel(ctx, wait_id, claim_epoch, schema_version):
    """Cancel a wait held by the current claim, preserving the result."""
    params = {"wait_id": wait_id}
    if schema_version:
        params["schema_version"] = int(schema_version)
    epoch = resolve_claim_epoch(claim_epoch)
    if epoch is not None:
        params["claim_epoch"] = epoch
    emit(ctx, _execute(ctx, "wait_cancel", params))


def _render_wait(record):
    lifecycle = record.get("lifecycle") or record.get("outcome", "pending")
    click.echo(f"Wait {record.get('wait_id', '—')} · {lifecycle}")
    owner = record.get("owner", {})
    if owner:
        click.echo(f"Owner: {owner.get('kind')}:{owner.get('id')}")
    age_seconds = record.get("age_seconds")
    age = (
        "age_unknown"
        if record.get("age_unknown") or age_seconds is None
        else f"{age_seconds:.0f}s"
    )
    click.echo(f"Age: {age}")
    click.echo("Verified evidence:")
    click.echo(json.dumps(record.get("verified", record.get("evidence", {})), indent=2))
    click.echo("Agent commentary:")
    click.echo(record.get("claimed") or "None")
    for label, key in (
        ("Sleep decision", "sleep"),
        ("Effective priority", "effective_priority"),
        ("Deadlines", "deadlines"),
        ("Blocker chain", "chain"),
        ("Escalation", "escalation"),
        ("Reservations", "reservations"),
        ("Continuation", "continuation"),
    ):
        if record.get(key):
            click.echo(f"{label}: {json.dumps(record[key], sort_keys=True)}")
    click.echo(f"Policy layer: {record.get('policy_layer', 'default')}")
    if record.get("chain_truncated"):
        click.echo("Blocker chain truncated.")


def _render_explain(payload):
    if payload.get("task"):
        task = payload["task"]
        click.echo(f"Task {task['id']}: {task['lifecycle']}")
        click.echo(f"Priority: base {task['base_priority']}, effective {task['effective_priority']}")
        if task.get("donor_task_id"):
            click.echo(f"Donor: {task['donor_task_id']} via {json.dumps(task.get('donor_path', []))}")
    records = [payload["explanation"]] if payload.get("explanation") else payload.get("waits", [])
    for record in records:
        _render_wait(record)
    if not records:
        click.echo("No active wait records.")
    if payload.get("truncated"):
        click.echo(f"Waits truncated: showing {len(records)} of {payload.get('total', 'unknown')}.")


def _render_graph(payload):
    graph = payload.get("graph") or payload
    for project in graph.get("projects", []):
        click.echo(f"Project {project['project_id']}: {project.get('total_waits', 0)} pending waits")
        for record in project.get("waits", []):
            _render_wait(record)
        for label, key in (
            ("Counts by kind and decision", "counts"),
            ("Longest chains", "longest_chains"),
            ("Deadlocks", "deadlocks"),
            ("Escalated waits", "escalated_waits"),
            ("Branch blockers and reservations", "branch_blockers"),
            ("Held message backlog", "held_message_backlog"),
        ):
            if project.get(key):
                click.echo(f"{label}: {json.dumps(project[key], sort_keys=True)}")
        truncated = [key for key, value in project.get("truncated", {}).items() if value]
        if truncated:
            click.echo(f"Project results truncated: {', '.join(truncated)}.")
    if not graph.get("projects"):
        click.echo("No project waits.")
    if graph.get("truncated"):
        click.echo("Project list truncated.")


@wait.command("explain")
@click.argument("wait_id", required=False)
@click.option("--task", "task_id", default=None)
@click.option("--project", "project_id", default=None)
@click.option("--history", is_flag=True)
@click.pass_context
@_handle_errors
def wait_explain(ctx, wait_id, task_id, project_id, history):
    """Read verified evidence first, then labelled agent commentary."""
    if bool(wait_id) == bool(task_id):
        raise click.UsageError("specify WAIT_ID or --task")
    params = {"wait_id": wait_id, "task_id": task_id, "project_id": project_id, "history": history}
    result = _execute(ctx, "wait_explain", {k: v for k, v in params.items() if v is not None})

    emit(ctx, result, render=_render_explain)


@wait.command("graph")
@click.option("--project", "project_id", default=None)
@click.option("--fleet", is_flag=True)
@click.pass_context
@_handle_errors
def wait_graph(ctx, project_id, fleet):
    """Read project blockers or the redacted fleet summary."""
    if fleet and project_id:
        raise click.UsageError("--project and --fleet are mutually exclusive")
    params = {"fleet": fleet}
    if project_id:
        params["project_id"] = project_id
    emit(ctx, _execute(ctx, "wait_graph", params), render=_render_graph)


@wait.command("checkpoint")
@click.option("--wait-id", required=True)
@click.option("--summary-file", type=click.Path(exists=True, dir_okay=False), required=True)
@click.option("--continuation-id", default=None)
@click.option("--generation", type=click.IntRange(min=1), default=None)
@claim_epoch_option
@click.pass_context
@_handle_errors
def wait_checkpoint(ctx, wait_id, summary_file, continuation_id, generation, claim_epoch):
    """Store the compaction summary on the active continuation."""
    params = dict(
        wait_id=wait_id,
        summary=Path(summary_file).read_text(encoding="utf-8"),
        continuation_id=continuation_id,
        generation=generation,
        claim_epoch=resolve_claim_epoch(claim_epoch),
    )
    emit(ctx, _execute(ctx, "wait_checkpoint", {k: v for k, v in params.items() if v is not None}))


@wait.command("ack")
@click.option("--wait-id", required=True)
@click.option("--continuation-id", default=None)
@click.option("--generation", type=click.IntRange(min=1), default=None)
@claim_epoch_option
@click.pass_context
@_handle_errors
def wait_ack(ctx, wait_id, continuation_id, generation, claim_epoch):
    """Acknowledge a delivered outcome using the current holder fence."""
    params = dict(
        wait_id=wait_id,
        continuation_id=continuation_id,
        generation=generation,
        claim_epoch=resolve_claim_epoch(claim_epoch),
    )
    emit(ctx, _execute(ctx, "wait_ack", {k: v for k, v in params.items() if v is not None}))


@wait.command("decide")
@click.option("--wait-id", required=True)
@click.option(
    "--decision",
    type=click.Choice(
        [
            "extend",
            "wake",
            "cancel",
            "reroute",
            "escalate_to_human",
        ]
    ),
    required=True,
)
@click.option("--note", required=True)
@click.option("--extend-seconds", type=click.FloatRange(min=60, max=86400), default=None)
@click.option("--to", default=None)
@click.option("--expected-generation", type=click.IntRange(min=1), default=None)
@click.option("--project", "project_id", default=None)
@click.pass_context
@_handle_errors
def wait_decide(ctx, **params):
    """Record a supervisor decision on an escalated wait."""
    emit(ctx, _execute(ctx, "wait_decide", {k: v for k, v in params.items() if v is not None}))


@wait.command("lift")
@click.option("--wait-id", required=True)
@click.option("--response", required=True, help="Supervisor response, or @file.")
@click.option("--expected-generation", type=click.IntRange(min=1), default=None)
@click.option("--project", "project_id", default=None)
@click.pass_context
@_handle_errors
def wait_lift(ctx, **params):
    """Lift a human wait with an explicit supervisor response."""
    if params["response"].startswith("@"):
        params["response"] = Path(params["response"][1:]).read_text(encoding="utf-8")
    emit(ctx, _execute(ctx, "wait_lift", {k: v for k, v in params.items() if v is not None}))


@wait.command("bridge")
@click.option("--project", "project_id", default=None)
@click.option("--apply", is_flag=True)
@click.pass_context
@_handle_errors
def wait_bridge(ctx, project_id, apply):
    """Operator-only projection of legacy waits; implementation must be installed."""
    params = {"apply": apply}
    if project_id:
        params["project_id"] = project_id
    emit(ctx, _execute(ctx, "wait_legacy_bridge", params))
