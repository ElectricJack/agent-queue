"""Read-only sweep of symptoms that can hide behind healthy task closes.

Run with ``aq doctor --check stall.sweep``.  The findings are deliberately
separate records in ``data`` so a supervisor can act on one without parsing a
human summary.  No probe in this module changes daemon, git or Docker state.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from pathlib import Path

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.models import ProjectStatus, TaskStatus

CHECK_ID = "stall.sweep"
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
    from src.doctor.integration_checks import _find_stranded_dependents

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
                "--format=%ct %h", "--author=Agent Queue", "--author=^aq ", timeout=5,
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
