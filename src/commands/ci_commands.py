"""CI baseline commands mixin for CommandHandler.

``ci_baseline_status`` is the observation half of the ``ci-main-sentinel``
playbook (``docs/superpowers/specs/2026-09-05-ci-main-sentinel-design.md``):
it reads the default branch head's check runs through ``gh``, judges them
with the same :func:`src.git.ci_gate.classify_rollup` the merge gate uses,
names the failing pytest node ids, and derives the repair task keyed by the
**failure signature** — so the playbook itself stays a deterministic command
graph with no prose or state of its own.

``ci_repair_adopt`` is its one write.  It makes a task *the* repair for a
failure: it keys the task and records the failing tests the repair owns, so
the next observation still recognises the repair after a partial fix shrinks
the failing set, and an operator can hand the sentinel a repair filed by hand
(``docs/superpowers/specs/2026-09-16-ci-sentinel-repair-coverage-design.md``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass

from src.git.ci_gate import GREEN, PENDING, RED, UNKNOWN, classify_rollup
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: Every repair attempt for one signature is a separate task keyed
#: ``ci-baseline:<signature>:<n>``; the human gate is keyed on the
#: signature alone so it is opened once.
REPAIR_KEY_PREFIX = "ci-baseline"
ESCALATION_KEY_PREFIX = "ci-baseline-escalation"
#: Task metadata holding the failure a repair owns (``ci_repair_adopt``).
REPAIR_RECORD_META = "ci_baseline_repair"
DEFAULT_MAX_ATTEMPTS = 2
#: Statuses that mean "this attempt is over and main is still red".
_SPENT_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED})
_MAX_LISTED_TESTS = 40


def failure_signature(failing_tests: list[str], failing_checks: list[str]) -> str:
    """A stable digest of *what* is red, independent of *which commit* is red.

    A new commit that leaves the same tests red is the same problem and must
    reuse the in-flight repair; a different set of failing tests is a new
    problem.  Falls back to the failing check names when no test ids could be
    read from the logs, so an unreadable log still yields one repair per
    distinct red matrix rather than none.
    """
    basis = sorted(failing_tests) or sorted(f"check:{name}" for name in failing_checks)
    payload = "\n".join(basis).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _key_parts(dedup_key: str | None) -> tuple[str, int] | None:
    """``(signature, n)`` of a ``ci-baseline:<signature>:<n>`` key, else ``None``."""
    parts = (dedup_key or "").split(":")
    if len(parts) != 3 or parts[0] != REPAIR_KEY_PREFIX:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return parts[1], 0


def _key_signature(dedup_key: str | None) -> str | None:
    parts = _key_parts(dedup_key)
    return parts[0] if parts else None


@dataclass(frozen=True)
class RepairAttempt:
    """One ``ci-baseline:*`` task as the sentinel's arithmetic sees it."""

    task_id: str
    dedup_key: str
    live: bool
    #: The ``REPAIR_RECORD_META`` value, or ``None`` for a repair filed before
    #: repairs were recorded — all such a repair is known to own is the exact
    #: failure its key's signature names.
    record: dict | None = None


@dataclass(frozen=True)
class RepairPlan:
    """What the sentinel should do about the failure it is looking at."""

    #: Live repairs that own some of the failure.
    in_flight: tuple[str, ...]
    #: The key of the in-flight repair that owns what is left, when one does.
    reuse_key: str | None
    #: The failure the (new or reused) repair owns: the uncovered tests, or
    #: the uncovered check names when no test ids could be read.
    tests: tuple[str, ...]
    checks: tuple[str, ...]
    signature: str
    #: Spent repairs that already owned all of that failure.
    prior_attempts: tuple[str, ...]
    #: The key a new repair is filed under.
    next_key: str


def _owned(
    attempt: RepairAttempt,
    *,
    ref: str,
    tests: frozenset[str],
    checks: frozenset[str],
    signatures: frozenset[str],
) -> frozenset[str]:
    """The part of a failure that *attempt* owns.

    The failure's items are its tests, or its check names when no test ids
    were read — the same basis :func:`failure_signature` digests.  A recorded
    repair owns the items it recorded for the same ref; a repair recorded
    while its logs were unreadable owns every test its checks turn red.  An
    unrecorded repair owns everything or nothing, by its key's signature.
    """
    basis = tests or checks
    if attempt.record is None:
        return basis if _key_signature(attempt.dedup_key) in signatures else frozenset()
    if attempt.record.get("ref") != ref:
        return frozenset()
    recorded_tests = frozenset(attempt.record.get("failing_tests") or ())
    recorded_checks = frozenset(attempt.record.get("failing_checks") or ())
    if not tests:
        return basis & recorded_checks
    if recorded_tests:
        return basis & recorded_tests
    return basis if checks <= recorded_checks else frozenset()


def plan_repair(
    *,
    ref: str,
    failing_tests: list[str],
    failing_checks: list[str],
    attempts: list[RepairAttempt],
) -> RepairPlan:
    """Decide which repair owns the current failure, and what a new one would own.

    Live repairs together cover the tests they own.  When they cover all of
    the failure, the oldest one keyed on exactly this failure — else the
    oldest owner — is the in-flight repair and is reused: a partial fix that
    shrinks the failing set is still the same problem.  Otherwise only the
    uncovered part is a new failure, and a new repair owns just that, so no
    two fixers work the same test.  Its prior attempts are the spent repairs
    that already owned all of it, which is what the escalation rule counts.
    """
    tests = frozenset(failing_tests)
    checks = frozenset(failing_checks)
    whole = failure_signature(list(tests), list(checks))
    live = [attempt for attempt in attempts if attempt.live]

    owners: list[RepairAttempt] = []
    covered: set[str] = set()
    for attempt in live:
        part = _owned(
            attempt, ref=ref, tests=tests, checks=checks, signatures=frozenset({whole})
        )
        if part:
            owners.append(attempt)
            covered |= part
    uncovered = (tests or checks) - covered

    if owners and not uncovered:
        reuse = next((a for a in owners if _key_signature(a.dedup_key) == whole), owners[0])
        part_tests, part_checks, signature = tests, checks, whole
    else:
        part_tests = uncovered if tests else frozenset()
        part_checks = checks if tests else uncovered
        signature = failure_signature(list(part_tests), list(part_checks))
        # An unrecorded repair keyed on exactly what is left owns it too.
        reuse = next(
            (
                a
                for a in live
                if a.record is None
                and a not in owners
                and _key_signature(a.dedup_key) == signature
            ),
            None,
        )
        if reuse is not None:
            owners.append(reuse)

    part = part_tests or part_checks
    prior = tuple(
        attempt.task_id
        for attempt in attempts
        if not attempt.live
        and part
        and _owned(
            attempt,
            ref=ref,
            tests=part_tests,
            checks=part_checks,
            signatures=frozenset({whole, signature}),
        )
        == part
    )
    used = [
        parts[1]
        for parts in (_key_parts(attempt.dedup_key) for attempt in attempts)
        if parts is not None and parts[0] == signature
    ]
    return RepairPlan(
        in_flight=tuple(attempt.task_id for attempt in owners),
        reuse_key=reuse.dedup_key if reuse is not None else None,
        tests=tuple(sorted(part_tests)),
        checks=tuple(sorted(part_checks)),
        signature=signature,
        prior_attempts=prior,
        next_key=f"{REPAIR_KEY_PREFIX}:{signature}:{max(used, default=0) + 1}",
    )


def render_repair_task(
    *,
    ref: str,
    head_sha: str | None,
    failing_checks: list[str],
    failing_tests: list[str],
    run_url: str | None,
    attempt: int,
    in_flight: list[str] | None = None,
) -> tuple[str, str]:
    """The title and description of the repair task the sentinel files.

    ``in_flight`` names live repairs that already own the rest of a red
    branch: the new repair is told to leave that part to them.
    """
    short = (head_sha or "unknown")[:8]
    title = f"Fix red CI on {ref} @ {short} (attempt {attempt})"
    listed = failing_tests[:_MAX_LISTED_TESTS]
    more = len(failing_tests) - len(listed)
    lines = [
        f"CI on `{ref}` is red at {head_sha or 'an unknown commit'}.",
        "",
        "Failing checks: " + (", ".join(failing_checks) or "none named"),
    ]
    if run_url:
        lines.append(f"Run: {run_url}")
    if listed:
        lines += ["", "Failing tests:"] + [f"- {test}" for test in listed]
        if more > 0:
            lines.append(f"- ... and {more} more")
    else:
        lines += ["", "No pytest node ids could be read from the job logs; read the run."]
    if in_flight:
        lines += [
            "",
            (
                "Other failures on this branch already belong to in-flight repair "
                f"task(s) {', '.join(in_flight)}. This task owns only the failures listed "
                "above; leave the rest to those tasks."
            ),
        ]
    lines += [
        "",
        "## What to do",
        f"Make `{ref}` green again with the smallest change that does it. Reproduce the",
        "failure at the head sha above first; if it does not reproduce, say so in the close",
        "summary with the evidence and do not change code. Fix the actual defect, not the",
        "test, unless the test is asserting something that is no longer true. Land the fix",
        "through a pull request against the default branch; never push to it directly.",
        "If this project uses an integration train, submit the repair PR for its normal",
        "review and train collection; do not merge the repair PR yourself. The train owns",
        "candidate CI, bounded repair, and promotion. Do not create a parallel merge path.",
        "Do not bundle unrelated work. If the failure is outside this repository's control",
        "(a runner outage, a rate limit), close with outcome fail and failure-class hard",
        "and say why, so the sentinel escalates to a human instead of filing another attempt.",
        "",
        "## Acceptance criteria",
        "- The failing checks above pass on the PR head"
        if not in_flight
        else "- The failing tests above pass on the PR head",
        "- The change touches only what the failure required",
        f"- This is attempt {attempt} for this failure signature",
    ]
    return title, "\n".join(lines)


def _string_list(value: object) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError
    return sorted({item.strip() for item in value if item.strip()})


class CiCommandsMixin:
    """Mixin that adds CI baseline reads to CommandHandler."""

    async def _observe_ci_baseline(self, project, ref: str) -> dict:
        """Read *ref*'s head check runs and, when red, its failing pytest node ids.

        Returns ``state``, ``head_sha``, ``failing_checks``, ``pending_checks``,
        ``failing_tests`` and ``run_url``, plus ``error`` when the verdict could
        not be read.
        """
        git = self.orchestrator.git
        if not project.repo_url:
            return {
                "state": UNKNOWN,
                "head_sha": None,
                "error": f"project {project.id} has no GitHub repo_url to read CI from",
            }
        from src.projects.github import GitHubError, parse_github_repository

        try:
            expected_name = parse_github_repository(project.repo_url).full_name
        except GitHubError:
            return {
                "state": UNKNOWN, "head_sha": None,
                "error": f"project {project.id} has no GitHub repo_url to read CI from",
            }
        try:
            repository = await git.bind_github_repository(project.repo_url)
            if repository.full_name != expected_name:
                raise ValueError("GitHub repository identity changed")
        except Exception:
            return {
                "state": UNKNOWN, "head_sha": None,
                "error": f"could not authorize GitHub repository for project {project.id}",
            }
        slug = repository.full_name
        cwd = self.config.data_dir or os.getcwd()
        os.makedirs(cwd, exist_ok=True)
        head_sha = await git.acommit_head_sha(slug, ref, cwd=cwd, repository=repository)
        entries = (
            await git.acommit_check_runs(slug, head_sha, cwd=cwd, repository=repository)
            if head_sha else None
        )
        verdict = classify_rollup(entries)
        observed = {
            "head_sha": head_sha,
            "state": verdict.state,
            "failing_checks": list(verdict.failing),
            "pending_checks": list(verdict.pending),
            "failing_tests": [],
            "run_url": None,
        }
        if verdict.state == UNKNOWN:
            observed["error"] = f"could not read check runs for {slug}@{ref}"
        if verdict.state != RED:
            return observed

        failing_tests: set[str] = set()
        run_url: str | None = None
        for entry in entries or []:
            if not isinstance(entry, dict) or entry.get("name") not in verdict.failing:
                continue
            run_url = run_url or entry.get("html_url") or entry.get("details_url")
            job_id = entry.get("id")
            if job_id is None:
                continue
            tests = await git.ajob_failed_tests(slug, job_id, cwd=cwd, repository=repository)
            failing_tests.update(tests or [])
        observed.update({"failing_tests": sorted(failing_tests), "run_url": run_url})
        return observed

    async def _ci_repair_attempts(self, project_id: str) -> list[RepairAttempt]:
        """Every ``ci-baseline:*`` repair in the project, oldest first, with its record."""
        tasks = await self.db.list_tasks_by_dedup_prefix(project_id, f"{REPAIR_KEY_PREFIX}:")
        tasks = [task for task in tasks if _key_parts(task.dedup_key) is not None]
        records = await self.db.get_task_meta_bulk(
            [task.id for task in tasks], REPAIR_RECORD_META
        )
        attempts = []
        for task in tasks:
            record = records.get(task.id)
            attempts.append(
                RepairAttempt(
                    task_id=task.id,
                    dedup_key=task.dedup_key,
                    live=task.status not in _SPENT_STATUSES,
                    record=record if isinstance(record, dict) else None,
                )
            )
        return attempts

    async def _cmd_ci_baseline_status(self, args: dict) -> dict:
        """Judge a branch head's CI and derive the repair task for it.

        Read-only.  Returns ``state`` (``green`` / ``red`` / ``pending`` /
        ``unknown``), the failing checks and pytest node ids, the failure
        ``signature``, and — when red — the ``dedup_key``, ``title`` and
        ``description`` of the repair task, plus ``escalated`` once
        ``max_attempts`` repair tasks that owned the failure have already
        completed or blocked while the branch stayed red.

        Live repairs own the failing tests they recorded (``ci_repair_adopt``).
        When they own every failing test, ``dedup_key`` is the in-flight
        repair's key; otherwise the repair fields describe a new repair for the
        uncovered tests alone (``repair_tests`` / ``repair_checks`` /
        ``repair_signature``), and ``in_flight`` names the repairs that own the
        rest.

        Args:
            project_id: Required — the project whose repository to read.
            ref: Branch or sha to judge; default the project's default branch.
            max_attempts: Repair attempts per failure before escalating (2).
        """
        project_id = str(args.get("project_id") or "").strip()
        if not project_id:
            return {"success": False, "error": "project_id is required"}
        project = await self.db.get_project(project_id)
        if project is None:
            return {"success": False, "error": f"unknown project: {project_id}"}
        ref = str(args.get("ref") or project.repo_default_branch or "main").strip()
        raw_max = args.get("max_attempts")
        try:
            max_attempts = DEFAULT_MAX_ATTEMPTS if raw_max is None else int(raw_max)
        except (TypeError, ValueError):
            return {"success": False, "error": "max_attempts must be an integer"}
        if max_attempts < 1:
            return {"success": False, "error": "max_attempts must be at least 1"}

        observed = await self._observe_ci_baseline(project, ref)
        result = {
            "success": True,
            "project_id": project_id,
            "ref": ref,
            "failing_checks": [],
            "pending_checks": [],
            "failing_tests": [],
            "run_url": None,
            **observed,
            "signature": None,
            "attempt": 0,
            "prior_attempts": [],
            "escalated": False,
            "in_flight": [],
        }
        if observed["state"] in (GREEN, PENDING, UNKNOWN):
            return result
        assert observed["state"] == RED

        head_sha = observed["head_sha"]
        failing_checks = observed["failing_checks"]
        tests_sorted = observed["failing_tests"]
        plan = plan_repair(
            ref=ref,
            failing_tests=tests_sorted,
            failing_checks=failing_checks,
            attempts=await self._ci_repair_attempts(project_id),
        )
        attempt = len(plan.prior_attempts) + 1
        escalated = plan.reuse_key is None and len(plan.prior_attempts) >= max_attempts
        title, description = render_repair_task(
            ref=ref,
            head_sha=head_sha,
            failing_checks=failing_checks,
            failing_tests=list(plan.tests),
            run_url=observed["run_url"],
            attempt=attempt,
            in_flight=list(plan.in_flight) if plan.reuse_key is None else None,
        )
        spent = ", ".join(plan.prior_attempts)
        result.update(
            {
                "signature": failure_signature(tests_sorted, failing_checks),
                "attempt": attempt,
                "prior_attempts": list(plan.prior_attempts),
                "escalated": escalated,
                "in_flight": list(plan.in_flight),
                "repair_signature": plan.signature,
                "repair_tests": list(plan.tests),
                "repair_checks": list(plan.checks),
                "dedup_key": plan.reuse_key or plan.next_key,
                "title": title,
                "description": description,
                "escalation_key": f"{ESCALATION_KEY_PREFIX}:{plan.signature}",
                "escalation_title": (
                    f"CI on {ref} still red after {len(plan.prior_attempts)} repair attempt(s)"
                ),
                "escalation_question": (
                    f"`{ref}` is red at {head_sha or 'an unknown commit'} with failure signature "
                    f"{plan.signature} ({', '.join(failing_checks) or 'no named checks'}). "
                    f"Repair attempts {spent or 'none'} did not "
                    "make it green. Decide: fix it by hand, retarget the repair, or accept the "
                    "red state; resolve this gate when done."
                ),
            }
        )
        return result

    async def _cmd_ci_repair_adopt(self, args: dict) -> dict:
        """Make a live task the repair that owns a red branch's failure.

        Keys an unkeyed task ``ci-baseline:<signature>:<n>`` and records the
        failure it owns in task metadata, so ``ci_baseline_status`` reuses it
        instead of filing another repair — including after a partial fix
        shrinks the failing set.  The record is written once: a task that is
        already keyed and recorded is returned ``unchanged``.

        Args:
            project_id: Required — the project the task belongs to.
            task_id: Required — the live task to adopt.
            ref: The branch the failure is on; default the project's default branch.
            head_sha: The commit the failure was read at, when the caller read it.
            failing_tests: The pytest node ids the repair owns.
            failing_checks: The failing check names.  With neither list given,
                the command reads ``ref``'s CI now and adopts its whole failure.
        """
        project_id = str(args.get("project_id") or "").strip()
        task_id = str(args.get("task_id") or "").strip()
        if not project_id or not task_id:
            return {"success": False, "error": "project_id and task_id are required"}
        project = await self.db.get_project(project_id)
        if project is None:
            return {"success": False, "error": f"unknown project: {project_id}"}
        task = await self.db.get_task(task_id)
        if task is None or task.project_id != project_id:
            return {"success": False, "error": f"task '{task_id}' not found in {project_id}"}
        if task.status in _SPENT_STATUSES:
            return {
                "success": False,
                "error": (
                    f"task '{task_id}' is {task.status.value}; only a live task can be the "
                    "in-flight CI repair"
                ),
            }
        key = task.dedup_key
        if key and _key_parts(key) is None:
            return {
                "success": False,
                "error": (
                    f"task '{task_id}' already carries dedup key '{key}', which is not a CI "
                    "repair key; it belongs to another pipeline"
                ),
            }
        record = await self.db.get_task_meta(task_id, REPAIR_RECORD_META)
        if key and isinstance(record, dict):
            return {
                "success": True,
                "outcome": "unchanged",
                "task_id": task_id,
                "dedup_key": key,
                "ref": record.get("ref"),
                "head_sha": record.get("head_sha"),
                "signature": record.get("signature"),
                "failing_tests": list(record.get("failing_tests") or []),
                "failing_checks": list(record.get("failing_checks") or []),
                "in_flight": [],
            }

        ref = str(args.get("ref") or project.repo_default_branch or "main").strip()
        head_sha = args.get("head_sha") or None
        try:
            failing_tests = _string_list(args.get("failing_tests"))
            failing_checks = _string_list(args.get("failing_checks"))
        except ValueError:
            return {
                "success": False,
                "error": "failing_tests and failing_checks must be lists of strings",
            }
        if failing_tests is None and failing_checks is None:
            observed = await self._observe_ci_baseline(project, ref)
            if observed["state"] != RED:
                return {
                    "success": False,
                    "error": observed.get("error")
                    or f"CI on {ref} is {observed['state']}; there is no failure to repair",
                }
            head_sha = observed["head_sha"]
            failing_tests = observed["failing_tests"]
            failing_checks = observed["failing_checks"]
        failing_tests = failing_tests or []
        failing_checks = failing_checks or []
        if not failing_tests and not failing_checks:
            return {
                "success": False,
                "error": "failing_tests or failing_checks must name the failure the task repairs",
            }

        signature = failure_signature(failing_tests, failing_checks)
        attempts = await self._ci_repair_attempts(project_id)
        if key:
            outcome = "recorded"
        else:
            used = [
                parts[1]
                for parts in (_key_parts(attempt.dedup_key) for attempt in attempts)
                if parts is not None and parts[0] == signature
            ]
            key = f"{REPAIR_KEY_PREFIX}:{signature}:{max(used, default=0) + 1}"
            # Key before record: a record on an unkeyed task is invisible to
            # the sentinel, while a keyed but unrecorded task still owns its
            # exact signature and is recorded by the next adopt.
            await self.db.update_task(task_id, dedup_key=key)
            outcome = "adopted"
        await self.db.set_task_meta(
            task_id,
            REPAIR_RECORD_META,
            {
                "ref": ref,
                "head_sha": head_sha,
                "signature": signature,
                "failing_tests": failing_tests,
                "failing_checks": failing_checks,
                "recorded_at": time.time(),
            },
        )
        if outcome == "adopted":
            adopted = await self.db.get_task(task_id)
            if adopted is not None:
                await self._emit_task_graph_change("task.updated", adopted)
        # Other live repairs owning part of this failure are duplicates the
        # operator may want to close; name them rather than guess which to keep.
        whole = frozenset({signature})
        others = [
            attempt.task_id
            for attempt in attempts
            if attempt.live
            and attempt.task_id != task_id
            and _owned(
                attempt,
                ref=ref,
                tests=frozenset(failing_tests),
                checks=frozenset(failing_checks),
                signatures=whole,
            )
        ]
        return {
            "success": True,
            "outcome": outcome,
            "task_id": task_id,
            "dedup_key": key,
            "ref": ref,
            "head_sha": head_sha,
            "signature": signature,
            "failing_tests": failing_tests,
            "failing_checks": failing_checks,
            "in_flight": others,
        }
