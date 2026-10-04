"""Read-only sweep of symptoms that can hide behind healthy task closes.

Run with ``aq doctor --check stall.sweep``.  The findings are deliberately
separate records in ``data`` so a supervisor can act on one without parsing a
human summary.  No probe in this module changes daemon, git or Docker state.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter
from pathlib import Path

from sqlalchemy import and_, or_, select

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.models import ProjectStatus, TaskStatus

CHECK_ID = "stall.sweep"
_STUCK_CHILD_AFTER_SECONDS = 5 * 60
_MAX_PR_PROBES = 20
_PARENT_PR_PROBE_SECONDS = 2.0
_PARENT_PR_PROBE_BUDGET_SECONDS = 10.0
_READY_AGE = 20 * 60
_DEFINED_AGE = 30 * 60
_DELIVERY_LAG = 45 * 60
_SESSION_IDLE = 15 * 60
_DELIVERY_AGE = 6 * 3600
_WAITING = {"blocked_dependency", "route_waiting_for_compatible_agent", "awaiting_pool_session"}
_PANE_TROUBLE = re.compile(
    r"usage limit|not logged in|login-required|log in|rate limit|hit your|"
    r"do you want to proceed|dangerous|^\s*[>$❯]\s*$", re.I
)
_DESIGN_HINT = re.compile(r"\b(design|spec|architecture|plan)\b", re.I)
_SKIP = re.compile(
    r"development publisher: skipping (?P<child>\S+) because dependency "
    r"(?P<blocker>\S+) is unavailable", re.I
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_VALIDATION_CONTAINER = "aq-triage-test-20260831"


def _finding(kind: str, project_id: str | None, detail: str, **data) -> dict:
    return {"kind": kind, "project_id": project_id, "detail": detail, **data}


async def _command(*args: str, timeout: float = 4) -> tuple[int, str]:
    """Bounded local read; a missing optional tool is represented by exit 127."""
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return 124, ""
        return process.returncode or 0, stdout.decode("utf-8", "replace")
    except OSError:
        return 127, ""


def _tail(path: Path, size: int = 400_000) -> str:
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - size))
            return stream.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _pane_issue(lines: list[str]) -> str:
    """Prefer the limit or confirmation text over a trailing bare prompt."""
    return next((line for line in reversed(lines) if not re.fullmatch(r"[>$❯]", line)), lines[-1])


class _Explains:
    def __init__(self, ctx: DoctorContext):
        self.ctx = ctx
        self.limit = asyncio.Semaphore(8)
        self.cache: dict[str, asyncio.Task[dict]] = {}

    async def get(self, task_id: str) -> dict:
        if task_id not in self.cache:
            self.cache[task_id] = asyncio.create_task(self._fetch(task_id))
        return await self.cache[task_id]

    async def _fetch(self, task_id: str) -> dict:
        async with self.limit:
            result = await self.ctx.handler.execute("explain_task", {"task_id": task_id})
            if not result.get("success"):
                raise RuntimeError(f"task explain {task_id}: {result.get('error', 'failed')}")
            return result


async def _work_findings(ctx: DoctorContext, tasks: list, now: float, explains: _Explains) -> list[dict]:
    candidates = [
        t for t in tasks
        if (t.status is TaskStatus.READY and now - t.updated_at > _READY_AGE)
        or (t.status is TaskStatus.DEFINED and now - t.updated_at > _DEFINED_AGE)
    ]
    # One daemon round trip per candidate, bounded to eight concurrent calls.
    explanations = await asyncio.gather(*(explains.get(t.id) for t in candidates))
    findings: list[dict] = []
    completions: dict[str, float | None] = {}
    for task, why in zip(candidates, explanations):
        reasons = why.get("reasons") or []
        deps = [r for r in reasons if r.get("code") == "blocked_dependency"]
        completed = [r for r in deps if "status=COMPLETED" in r.get("detail", "")]
        # An unfinished dependency is normal queued work.  If a completed
        # blocker is present, check its own completion age before alarming.
        if deps and not completed:
            continue
        old_completed = []
        for reason in completed:
            blocker = reason.get("ref")
            if not blocker:
                continue
            if blocker not in completions:
                record = await ctx.db.get_task_completion(blocker)
                completions[blocker] = record.completed_at if record else None
            done = completions[blocker]
            if done is None or now - done >= _DELIVERY_LAG:
                old_completed.append(blocker)
        if completed and not old_completed and all(r.get("code") in _WAITING for r in reasons):
            continue
        if deps and not old_completed and all(r.get("code") in _WAITING for r in reasons):
            continue
        kind = "undelivered_blocker" if old_completed else "unclaimed_work"
        findings.append(_finding(
            kind, task.project_id,
            f"{task.id} has been {task.status.value} for {int((now-task.updated_at)/60)}m; "
            + "; ".join(f"{r.get('code')}: {r.get('detail', '')}" for r in reasons[:2]),
            task_id=task.id, blockers=old_completed, reasons=reasons[:5],
        ))
    return findings


def _route_findings(tasks: list) -> list[dict]:
    """Preserve the local sweep's high-cost routing sanity check."""
    return [
        _finding(
            "non_design_expensive_route", task.project_id,
            f"{task.id} [{task.status.value}] uses deep-high-claude for {task.title[:70]}",
            task_id=task.id,
        )
        for task in tasks
        if task.status in {TaskStatus.READY, TaskStatus.DEFINED}
        and task.profile_id == "deep-high-claude"
        and getattr(task.task_type, "value", task.task_type) not in {"plan", "research"}
        and not _DESIGN_HINT.search(task.title)
    ]


async def _branch_findings(ctx: DoctorContext, active: set[str], explains: _Explains) -> list[dict]:
    # The integration check already proves a receipt named the delivered SHA
    # and branch cleanup deleted it.  Explain provides the final authority:
    # an unclaimed dependent with no blocked_dependency reason is not stranded.

    candidates = [
        item for item in await _find_stranded_dependents(
            ctx,
            candidate_statuses=(TaskStatus.DEFINED, TaskStatus.READY, TaskStatus.COMPLETED),
        )
        if item["project_id"] in active
    ]
    explanations = await asyncio.gather(
        *(explains.get(item["dependent_task_id"]) for item in candidates)
    )
    return [
        _finding(
            "restore_branch", item["project_id"],
            f"{item['dependent_task_id']} is blocked by delivered {item['blocker_task_id']}; "
            f"restore its branch at {item['source_sha']}",
            **{key: value for key, value in item.items() if key != "project_id"},
        )
        for item, why in zip(candidates, explanations)
        if any(
            r.get("code") == "blocked_dependency"
            and r.get("ref") == item["blocker_task_id"]
            for r in why.get("reasons") or []
        )
    ]


async def _session_findings(ctx: DoctorContext, active: set[str], tasks: list, now: float) -> list[dict]:
    sessions = await ctx.db.list_sessions()
    ready = Counter(t.profile_id for t in tasks if t.status is TaskStatus.READY)
    findings: list[dict] = []
    idle_sessions = [
        session for session in sessions
        if session.project_id in active
        and session.state in {"running", "starting"}
        and now - (session.last_activity or session.started_at) >= _SESSION_IDLE
    ]
    pane_sessions = [
        session for session in idle_sessions
        if (session.task_id and session.lifecycle != "named")
        or (session.lifecycle == "pool" and ready[session.profile_id])
    ]
    limit = asyncio.Semaphore(8)

    async def capture(session):
        async with limit:
            _, output = await _command("tmux", "capture-pane", "-p", "-t", session.name)
            return session.name, output

    panes = dict(await asyncio.gather(*(capture(session) for session in pane_sessions)))
    for session in idle_sessions:
        idle = now - (session.last_activity or session.started_at)
        if session.task_id and session.lifecycle != "named":
            pane = panes.get(session.name, "")
            trouble = [line.strip() for line in pane.splitlines()[-30:] if _PANE_TROUBLE.search(line)]
            findings.append(_finding(
                "idle_worker", session.project_id,
                f"{session.name} holds {session.task_id}, idle {int(idle/60)}m"
                + (f"; pane: {_pane_issue(trouble)[:120]}" if trouble else ""),
                session_name=session.name, task_id=session.task_id,
            ))
        elif session.lifecycle == "pool" and ready[session.profile_id]:
            pane = panes.get(session.name, "")
            trouble = [line.strip() for line in pane.splitlines()[-30:] if _PANE_TROUBLE.search(line)]
            if trouble:
                findings.append(_finding(
                    "idle_pool", session.project_id,
                    f"{session.name} idle {int(idle/60)}m with {ready[session.profile_id]} "
                    f"READY on {session.profile_id}; pane: {_pane_issue(trouble)[:120]}",
                    session_name=session.name, ready=ready[session.profile_id],
                ))
    recent = Counter(
        s.name for s in sessions
        if s.project_id in active and s.name.startswith("s-") and now-s.started_at < 3600
    )
    findings.extend(
        _finding("crash_loop", None, f"{name} started {count} times in the last hour",
                 session_name=name, count=count)
        for name, count in sorted(recent.items()) if count >= 5
    )
    return findings


def _log_findings(ctx: DoctorContext, active: set[str], tasks: list) -> list[dict]:
    data_dir = Path(ctx.config.data_dir).expanduser()
    publisher_log = Path(ctx.config.logging.log_file).expanduser() if ctx.config.logging.log_file else data_dir / "logs" / "agent-queue.log"
    lines = _tail(publisher_log).splitlines()
    by_id = {task.id: task for task in tasks}
    skips = {}
    for line in lines:
        match = _SKIP.search(line)
        if match and match["child"] in by_id:
            skips[match["child"]] = match["blocker"]
    findings = [
        _finding("publisher_skip", by_id[child].project_id,
                 f"publisher skips {child} because dependency {blocker} is unavailable",
                 task_id=child, blocker_id=blocker)
        for child, blocker in sorted(skips.items())
        if by_id[child].project_id in active
    ]
    daemon_lines = _ANSI.sub("", _tail(data_dir / "daemon.log")).splitlines()
    for marker in ("development publisher tick failed", "Development batch remains recoverable"):
        hits = [line for line in daemon_lines if marker in line]
        if hits:
            findings.append(_finding("publisher_error", None, hits[-1][:200]))
    return findings


async def _aq_committer_filters(ctx: DoctorContext, project_id: str) -> list[str]:
    """``--committer`` patterns matching commits AQ made for *project_id*.

    AQ commits as the project's resolved Git identity (git-identity spec),
    which may be a person's own address, so delivery is read from the
    committer rather than guessed from an author name; earlier releases'
    fixed identities still count.
    """
    from src.git.identity import resolve_git_identity

    project = await ctx.db.get_project(project_id)
    email = resolve_git_identity(ctx.config, project).identity.email
    # Fixed strings: an address such as ``123+me@users.noreply.github.com``
    # is not a regular expression.
    return [
        "--fixed-strings",
        f"--committer=<{email}>",
        "--committer=@agent-queue.local>",
        "--committer=<aq@localhost>",
        "--committer=<agent-queue@localhost>",
    ]


async def _delivery_findings(ctx: DoctorContext, active: set[str], tasks: list, now: float) -> list[dict]:
    repos = await ctx.db.list_repos()
    pending = Counter(t.project_id for t in tasks if t.status is TaskStatus.DEFINED)
    candidates = [
        repo for repo in repos
        if repo.project_id in active and pending[repo.project_id]
        and (repo.checkout_base_path or repo.source_path)
    ]
    limit = asyncio.Semaphore(8)

    async def last_delivery(repo):
        checkout = repo.checkout_base_path or repo.source_path
        async with limit:
            code, output = await _command(
                "git", "-C", checkout, "log", f"origin/{repo.default_branch}", "-1",
                "--format=%ct %h", *await _aq_committer_filters(ctx, repo.project_id),
                timeout=5,
            )
        return repo, checkout, code, output

    findings = []
    for repo, checkout, code, output in await asyncio.gather(
        *(last_delivery(repo) for repo in candidates)
    ):
        fields = output.split()
        if code == 0 and len(fields) >= 2 and fields[0].isdigit():
            age = now - int(fields[0])
            if age > _DELIVERY_AGE:
                findings.append(_finding(
                    "delivery_stale", repo.project_id,
                    f"last worker commit on origin/{repo.default_branch} was "
                    f"{int(age/3600)}h ago ({fields[1]}); {pending[repo.project_id]} DEFINED tasks",
                    checkout=checkout, commit=fields[1],
                ))
    return findings


async def _provider_findings(ctx: DoctorContext, tasks: list) -> list[dict]:
    rows = await ctx.db.list_provider_availability()
    disabled = {r["provider"] for r in rows if r["state"] == "disabled"}
    if not disabled:
        return []
    profiles = {profile.id: profile for profile in await ctx.db.list_profiles()}
    findings = []
    for task in tasks:
        if task.status not in {TaskStatus.READY, TaskStatus.DEFINED}:
            continue
        profile = profiles.get(task.profile_id)
        provider = profile.harness if profile else None
        if provider in disabled:
            findings.append(_finding(
                "disabled_provider", task.project_id,
                f"{task.id} routes to disabled provider {provider} via {task.profile_id}",
                task_id=task.id, provider=provider,
            ))
    return findings




async def _check_sweep(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None or ctx.handler is None:
        return CheckResult(CHECK_ID, Severity.INFO, "database and command handler required")
    now = time.time()
    projects = await ctx.db.list_projects(status=ProjectStatus.ACTIVE)
    active = {project.id for project in projects}
    if not active:
        return CheckResult(CHECK_ID, Severity.OK, "no active projects", data={"findings": []})
    tasks_by_project = await asyncio.gather(
        *(ctx.db.list_tasks(project_id=pid) for pid in sorted(active))
    )
    tasks = [task for group in tasks_by_project for task in group]
    explains = _Explains(ctx)
    # Independent reads run together.  The explanation calls within them are
    # additionally capped at eight to keep the complete sweep under a minute.
    groups = await asyncio.gather(
        _work_findings(ctx, tasks, now, explains),
        _branch_findings(ctx, active, explains),
        _session_findings(ctx, active, tasks, now),
        _delivery_findings(ctx, active, tasks, now),
        _provider_findings(ctx, tasks),
        _unadmitted_parent_findings(ctx, active),
        _reviewed_file_guard_findings(ctx, active),
        _orphaned_pr_findings(ctx, active, now),
        asyncio.to_thread(_log_findings, ctx, active, tasks),
        _validation_findings(),
    )
    findings = [item for group in groups for item in group] + _route_findings(tasks)
    summary = (
        f"{len(findings)} stall finding(s) across {len(active)} active project(s)" if findings
        else f"no stalls across {len(active)} active project(s)"
    )
    detail = summary + "".join(
        f"\n{item['kind']} {item['project_id'] or '-'}: {item['detail']}"
        for item in findings
    )
    return CheckResult(
        CHECK_ID, Severity.WARN if findings else Severity.OK,
        detail,
        data={"findings": findings, "count": len(findings),
              "vault_root": str(Path(ctx.config.vault_root).expanduser())},
    )


async def _reviewed_file_guard_findings(ctx: DoctorContext, active: set[str]) -> list[dict]:

    return [
        _finding(
            "reviewed_file_guard",
            row["project_id"],
            f"batch {row['batch_id']} repair blocked by {row['invariant']}; "
            f"inspect aq integration status {row['project_id']} and resolve the "
            "durable Subject's policy gate before rebuilding",
            **{key: value for key, value in row.items() if key != "project_id"},
        )
        for row in await _find_reviewed_file_blocked_batches(ctx)
        if row["project_id"] in active
    ]


async def _orphaned_pr_findings(ctx: DoctorContext, active: set[str], now: float) -> list[dict]:

    pulls, errors = await _find_orphaned_prs(ctx, active, now=now)
    return [
        _finding("orphaned_pr", pull["project_id"],
                 f"{pull['pr_url']} ({pull['branch']}) is {int(pull['age_seconds'] / 3600)}h "
                 "old with no live task or train owner", **{
                     key: value for key, value in pull.items() if key != "project_id"
                 })
        for pull in pulls
    ] + [
        _finding("pr_inventory_failed", item["project_id"], item["error"])
        for item in errors
    ]


async def _unadmitted_parent_findings(ctx: DoctorContext, active: set[str]) -> list[dict]:

    return [
        _finding("unadmitted_parent", row["project_id"],
                 f"{row['task_id']} completed with PR {row['pr_url']}: {row['reason']}; "
                 "inspect the parent subject and its exact review evidence",
                 **{key: value for key, value in row.items() if key != "project_id"})
        for row in await _find_unadmitted_parents(ctx) if row["project_id"] in active
    ]


async def _validation_findings() -> list[dict]:
    code, output = await _command(
        "docker", "inspect", "-f", "{{.State.Status}}", _VALIDATION_CONTAINER,
    )
    state = output.strip() if code == 0 else "missing"
    if state == "running":
        return []
    return [_finding("validation_db", None, f"{_VALIDATION_CONTAINER} is {state}",
                     container=_VALIDATION_CONTAINER, state=state)]


def stall_checks() -> list[DoctorCheck]:
    return [DoctorCheck(id=CHECK_ID, run=_check_sweep, owner="operations", timeout_s=55)]


async def _parent_pr_observation(ctx: DoctorContext, row: dict) -> dict:
    """Use the configured repository credential; unavailable reads stay unknown."""
    git = getattr(getattr(ctx.handler, "orchestrator", None), "git", None)
    if git is None or not row["repo_url"]:
        return {"pr_open": None, "pr_head": None}
    try:
        binding = await git.bind_github_repository(row["repo_url"])
        pull = await git._github_client(binding).pull_request(row["pr_url"])
        head = pull.get("head") or {}
        base = pull.get("base") or {}
        canonical = (
            head.get("ref") == row["branch_name"]
            and (head.get("repo") or {}).get("id") == binding.repository_id
            and base.get("ref") == row["default_branch"]
            and (base.get("repo") or {}).get("id") == binding.repository_id
        )
        return {"pr_open": pull.get("state") == "open", "pr_head": head.get("sha"),
                "pr_canonical": canonical}
    except Exception:
        return {"pr_open": None, "pr_head": None}


async def _find_unadmitted_parents(ctx: DoctorContext) -> list[dict]:
    """Completed aggregate PRs invisible to the frontier, with exact blockers."""
    from sqlalchemy import exists

    from src.database import tables as t
    from src.database.queries.integration_train_queries import (
        _ACTIVE_BATCH_LIFECYCLES,
        _root_delivery_receipt_conditions,
    )
    from src.integration.review_evidence import ReviewEvidenceProducer

    child, archived = t.tasks.alias("unadmitted_child"), t.archived_tasks.alias("unadmitted_archived")
    statement = select(
        t.tasks.c.id.label("task_id"), t.tasks.c.project_id, t.tasks.c.pr_url,
        t.tasks.c.branch_name, t.tasks.c.task_type,
        t.repos.c.url.label("repo_url"), t.repos.c.default_branch,
        t.projects.c.hierarchical_integration_generation.label("policy_generation"),
        t.task_integration_checkpoints.c.checkpoint_sha,
        t.task_integration_checkpoints.c.current_verification_id,
        t.task_integration_checkpoints.c.last_completed_verification_id,
    ).select_from(t.tasks.join(t.projects, t.projects.c.id == t.tasks.c.project_id).join(
        t.repos, and_(t.repos.c.id == t.tasks.c.repo_id, t.repos.c.project_id == t.tasks.c.project_id),
    ).outerjoin(t.task_integration_checkpoints,
                t.task_integration_checkpoints.c.task_id == t.tasks.c.id)).where(
        t.projects.c.status == "ACTIVE", t.projects.c.hierarchical_integration_mode == "train",
        t.projects.c.integration_repository_id == t.tasks.c.repo_id,
        t.tasks.c.parent_task_id.is_(None), t.tasks.c.status == "COMPLETED",
        t.tasks.c.pr_url.is_not(None), t.tasks.c.pr_url != "",
        t.tasks.c.updated_at < time.time() - _STUCK_CHILD_AFTER_SECONDS,
        or_(exists(select(child.c.id).where(child.c.parent_task_id == t.tasks.c.id)),
            exists(select(archived.c.id).where(archived.c.parent_task_id == t.tasks.c.id))),
        ~exists(select(t.integration_batch_members.c.task_id).join(
            t.integration_batches, t.integration_batches.c.id == t.integration_batch_members.c.batch_id,
        ).where(t.integration_batch_members.c.task_id == t.tasks.c.id,
                t.integration_batches.c.lifecycle.in_(_ACTIVE_BATCH_LIFECYCLES))),
        ~exists(select(t.task_delivery_receipts.c.id).where(
            t.task_delivery_receipts.c.source_task_id == t.tasks.c.id,
            *_root_delivery_receipt_conditions(t.repos.c.id, t.repos.c.default_branch),
        )),
    ).order_by(t.tasks.c.updated_at, t.tasks.c.id).limit(100)
    producer = ReviewEvidenceProducer(ctx.db, None)
    findings = []
    probe_deadline = time.monotonic() + _PARENT_PR_PROBE_BUDGET_SECONDS
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(statement)).mappings().all()
        for index, record in enumerate(rows):
            row = dict(record)
            observed = {"pr_open": None, "pr_head": None}
            remaining = probe_deadline - time.monotonic()
            if index < _MAX_PR_PROBES and remaining > 0:
                try:
                    async with asyncio.timeout(min(_PARENT_PR_PROBE_SECONDS, remaining)):
                        observed = await _parent_pr_observation(ctx, row)
                except TimeoutError:
                    pass  # Local findings remain visible when GitHub cannot answer.
            if observed["pr_open"] is False:
                continue
            source = await producer._pull_request_source_on(conn, row["task_id"])
            review = None
            if source:
                review = (await conn.execute(select(t.integration_review_evidence).where(
                    t.integration_review_evidence.c.source_task_id == row["task_id"],
                    t.integration_review_evidence.c.repository_id == source["repository_id"],
                    t.integration_review_evidence.c.source_base == source["base"],
                    t.integration_review_evidence.c.reviewed_head_sha == source["head"],
                    t.integration_review_evidence.c.generation == source["generation"],
                ).order_by(t.integration_review_evidence.c.created_at.desc(),
                           t.integration_review_evidence.c.id.desc()).limit(1))).mappings().first()
            if observed.get("pr_canonical") is False:
                reason = "pr_identity_changed"
            elif observed["pr_head"] and observed["pr_head"] != row["checkpoint_sha"]:
                reason = "parent_head_moved"
            elif source is None:
                reason = "aggregate_verification_missing_or_stale"
            elif review is not None and review["verdict"] == "rejected":
                reason = "parent_review_rejected"
            elif review is None and not await producer._authorization_on(
                conn, row["task_id"], source, row["policy_generation"],
            ):
                reason = "parent_admission_not_authorized"
            elif (review is None or review["review_kind"] != "parent"
                  or (review["evidence"] or {}).get("verification_id") != source["verification_id"]):
                reason = "exact_parent_review_missing"
            else:
                reason = "awaiting_train_admission"
            findings.append({**row, **observed, "reason": reason,
                             "review_evidence_id": review["id"] if review else None})
    return findings


async def _find_stranded_dependents(
    ctx: DoctorContext,
    *,
    candidate_statuses: tuple[TaskStatus, ...] = (TaskStatus.COMPLETED,),
) -> list[dict]:
    """Find candidates held behind a cleaned-up commits-less delivery.

    A delivered manifest normally makes its source durable enough to survive
    branch cleanup. Older publisher builds consulted an empty completion first,
    however, and silently marked that blocker unavailable. The cleanup record
    is the durable evidence that this is that specific failure mode; a merely
    completed task with no commits is not enough to warrant an alarm.  Whether
    the dependent itself is already delivered is git's answer.
    """

    from src.database.tables import (
        projects,
        task_completion_records,
        task_dependencies,
        tasks,
    )

    async with ctx.db._engine.connect() as conn:
        development = set(
            (
                await conn.execute(
                    select(projects.c.id).where(
                        projects.c.hierarchical_integration_mode == "development"
                    )
                )
            ).scalars()
        )
        if not development:
            return []
        candidates = (
            (
                await conn.execute(
                    select(tasks.c.id, tasks.c.project_id)
                    .where(
                        tasks.c.project_id.in_(development),
                        tasks.c.status.in_(tuple(status.value for status in candidate_statuses)),
                    )
                )
            )
            .mappings()
            .all()
        )
        candidate_projects = {row["id"]: row["project_id"] for row in candidates}
        if not candidate_projects:
            return []
        links = (
            (
                await conn.execute(
                    select(task_dependencies.c.task_id, task_dependencies.c.depends_on_task_id)
                    .where(
                        task_dependencies.c.task_id.in_(candidate_projects),
                        task_dependencies.c.dep_type.in_(("blocks", "waits-for", "conditional-blocks")),
                    )
                )
            )
            .mappings()
            .all()
        )
        blocker_ids = {link["depends_on_task_id"] for link in links}
        if not blocker_ids:
            return []
        blockers = {
            row["id"]: row
            for row in (
                (
                    await conn.execute(
                        select(tasks.c.id, tasks.c.project_id, tasks.c.status)
                        .where(tasks.c.id.in_(blocker_ids))
                    )
                )
                .mappings()
                .all()
            )
        }
        completions = {}
        for row in (
            (
                await conn.execute(
                    select(
                        task_completion_records.c.task_id,
                        task_completion_records.c.id,
                        task_completion_records.c.commits,
                    )
                    .where(task_completion_records.c.task_id.in_(blocker_ids))
                    .order_by(
                        task_completion_records.c.task_id,
                        task_completion_records.c.completed_at.desc(),
                        task_completion_records.c.id.desc(),
                    )
                )
            )
            .mappings()
            .all()
        ):
            completions.setdefault(row["task_id"], row)
        from src.integration.development import operation_rows_on
        rows = await operation_rows_on(conn, development)


    stranded = {}
    for row in rows:
        cleanup = (row["evidence"] or {}).get("branch_cleanup") or {}
        deleted = cleanup.get("deleted") or []
        if not deleted:
            continue
        for member in row["manifest"] or []:
            blocker_id = member.get("task_id")
            source_sha = member.get("source_sha")
            blocker = blockers.get(blocker_id)
            completion = completions.get(blocker_id)
            if (
                blocker is None
                or blocker["project_id"] != row["project_id"]
                or blocker["status"] != TaskStatus.COMPLETED.value
                or completion is None
                or json.loads(completion["commits"])
                or not any(entry.get("sha") == source_sha for entry in deleted)
            ):
                continue
            for link in links:
                if link["depends_on_task_id"] != blocker_id:
                    continue
                dependent_id = link["task_id"]
                if candidate_projects.get(dependent_id) != row["project_id"]:
                    continue
                key = (dependent_id, blocker_id)
                stranded.setdefault(
                    key,
                    {
                        "project_id": row["project_id"],
                        "dependent_task_id": dependent_id,
                        "blocker_task_id": blocker_id,
                        "delivery_id": row["id"],
                        "source_sha": source_sha,
                    },
                )
    # A dependent whose own work git already finds on its target is not
    # stranded, whatever its blocker's history.  Git answers that, not a
    # delivery row; without an observer nothing is proven and all are listed.
    observer = getattr(ctx.db, "_delivery_observer", None)
    dependents = {item["dependent_task_id"] for item in stranded.values()}
    if observer is not None and dependents:
        from src.integration.delivery_truth import DeliveryState

        view = await observer.observe(dependents)
        stranded = {
            key: item for key, item in stranded.items()
            if not (
                (evidence := view.get(item["dependent_task_id"])) is not None
                and evidence.state is DeliveryState.CONTAINED
            )
        }
    return sorted(stranded.values(), key=lambda item: (item["project_id"], item["dependent_task_id"]))


async def _find_reviewed_file_blocked_batches(ctx: DoctorContext) -> list[dict]:

    from src.database.tables import (
        integration_batches,
        integration_repair_operations,
        integration_repair_stages,
    )
    from src.integration.migration_heads import REVIEWED_FILE_GUARDS

    batch, operation, stage = (
        integration_batches,
        integration_repair_operations,
        integration_repair_stages,
    )
    async with ctx.db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(
                        batch.c.id.label("batch_id"),
                        batch.c.project_id,
                        batch.c.current_revision,
                        operation.c.id.label("operation_id"),
                        stage.c.repair_task_id,
                        stage.c.dossier,
                    )
                    .select_from(
                        batch.join(operation, operation.c.batch_id == batch.c.id).join(
                            stage,
                            (stage.c.operation_id == operation.c.id)
                            & (stage.c.ordinal == operation.c.active_stage),
                        )
                    )
                    .where(
                        batch.c.lifecycle.in_(("repairing", "human_blocked")),
                        operation.c.state.in_(("active", "escalated", "human_required")),
                        stage.c.dossier["reviewed_file_guard"]["invariant"]
                        .as_string()
                        .in_(REVIEWED_FILE_GUARDS),
                    )
                    .order_by(batch.c.id)
                )
            )
            .mappings()
            .all()
        )
    findings = []
    for row in rows:
        guard = row["dossier"]["reviewed_file_guard"]
        if guard.get("revision") != row["current_revision"]:
            continue
        findings.append(
            {key: row[key] for key in ("batch_id", "project_id", "operation_id", "repair_task_id")}
            | guard
        )
    return findings


async def _find_orphaned_prs(ctx, project_ids=None, *, now=None):

    from src.database.tables import projects
    from src.git.github import GitHubAccess
    from src.git.manager import GitManager
    from src.integration.pr_cleanup import orphaned_pull_requests

    if ctx.db is None or not hasattr(ctx.db, "_engine"):
        return [], []
    factory = getattr(ctx.handler, "_integration_promotion_service", None)
    if factory is not None:
        git = factory().git
    else:
        integration = getattr(ctx.config, "integration", None)
        git = GitManager(github_access=GitHubAccess.from_config(
            getattr(integration, "github_app", None),
        ))
    statement = select(projects.c.id).where(projects.c.integration_repository_id.is_not(None))
    if project_ids is not None:
        statement = statement.where(projects.c.id.in_(project_ids))
    async with ctx.db._engine.connect() as conn:
        ids = list((await conn.execute(statement.order_by(projects.c.id))).scalars())
    findings, errors = [], []
    for project_id in ids:
        try:
            findings.extend(await orphaned_pull_requests(ctx.db, git, project_id, now=now))
        except Exception as exc:
            errors.append({"project_id": project_id, "error": str(exc)})
    return findings, errors
