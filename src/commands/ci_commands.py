"""CI baseline commands mixin for CommandHandler.

``ci_baseline_status`` is the observation half of the ``ci-main-sentinel``
playbook (``docs/superpowers/specs/2026-09-05-ci-main-sentinel-design.md``):
it reads the default branch head's check runs through ``gh``, judges them
with the same :func:`src.git.ci_gate.classify_rollup` the merge gate uses,
names the failing pytest node ids, and derives the repair task keyed by the
**failure signature** — so the playbook itself stays a deterministic command
graph with no prose or state of its own.
"""

from __future__ import annotations

import hashlib
import logging
import os

from src.git.ci_gate import GREEN, PENDING, RED, UNKNOWN, classify_rollup
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: Every repair attempt for one signature is a separate task keyed
#: ``ci-baseline:<signature>:<n>``; the human gate is keyed on the
#: signature alone so it is opened once.
REPAIR_KEY_PREFIX = "ci-baseline"
ESCALATION_KEY_PREFIX = "ci-baseline-escalation"
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


def render_repair_task(
    *,
    ref: str,
    head_sha: str | None,
    failing_checks: list[str],
    failing_tests: list[str],
    run_url: str | None,
    attempt: int,
) -> tuple[str, str]:
    """The title and description of the repair task the sentinel files."""
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
    lines += [
        "",
        "## What to do",
        f"Make `{ref}` green again with the smallest change that does it. Reproduce the",
        "failure at the head sha above first; if it does not reproduce, say so in the close",
        "summary with the evidence and do not change code. Fix the actual defect, not the",
        "test, unless the test is asserting something that is no longer true. Land the fix",
        "through a pull request against the default branch; never push to it directly.",
        "Do not bundle unrelated work. If the failure is outside this repository's control",
        "(a runner outage, a rate limit), close with outcome fail and failure-class hard",
        "and say why, so the sentinel escalates to a human instead of filing another attempt.",
        "",
        "## Acceptance criteria",
        "- The failing checks above pass on the PR head",
        "- The change touches only what the failure required",
        f"- This is attempt {attempt} for this failure signature",
    ]
    return title, "\n".join(lines)


class CiCommandsMixin:
    """Mixin that adds CI baseline reads to CommandHandler."""

    async def _cmd_ci_baseline_status(self, args: dict) -> dict:
        """Judge a branch head's CI and derive the repair task for it.

        Read-only.  Returns ``state`` (``green`` / ``red`` / ``pending`` /
        ``unknown``), the failing checks and pytest node ids, the failure
        ``signature``, and — when red — the ``dedup_key``, ``title`` and
        ``description`` of the repair task, plus ``escalated`` once
        ``max_attempts`` repair tasks for that signature have already
        completed or blocked while the branch stayed red.

        Args:
            project_id: Required — the project whose repository to read.
            ref: Branch or sha to judge; default the project's default branch.
            max_attempts: Repair attempts per signature before escalating (2).
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

        git = self.orchestrator.git
        slug = git.github_repo_slug(project.repo_url)
        base = {"success": True, "project_id": project_id, "ref": ref}
        if slug is None:
            return {
                **base,
                "state": UNKNOWN,
                "head_sha": None,
                "error": f"project {project_id} has no GitHub repo_url to read CI from",
            }
        cwd = self.config.data_dir or os.getcwd()
        os.makedirs(cwd, exist_ok=True)
        head_sha = await git.acommit_head_sha(slug, ref, cwd=cwd)
        entries = await git.acommit_check_runs(slug, head_sha, cwd=cwd) if head_sha else None
        verdict = classify_rollup(entries)
        result = {
            **base,
            "head_sha": head_sha,
            "state": verdict.state,
            "failing_checks": list(verdict.failing),
            "pending_checks": list(verdict.pending),
            "failing_tests": [],
            "run_url": None,
            "signature": None,
            "attempt": 0,
            "prior_attempts": [],
            "escalated": False,
        }
        if verdict.state in (GREEN, PENDING, UNKNOWN):
            if verdict.state == UNKNOWN:
                result["error"] = f"could not read check runs for {slug}@{ref}"
            return result
        assert verdict.state == RED

        failing_tests: set[str] = set()
        run_url: str | None = None
        for entry in entries or []:
            if not isinstance(entry, dict) or entry.get("name") not in verdict.failing:
                continue
            run_url = run_url or entry.get("html_url") or entry.get("details_url")
            job_id = entry.get("id")
            if job_id is None:
                continue
            tests = await git.ajob_failed_tests(slug, job_id, cwd=cwd)
            failing_tests.update(tests or [])
        tests_sorted = sorted(failing_tests)
        signature = failure_signature(tests_sorted, list(verdict.failing))
        prefix = f"{REPAIR_KEY_PREFIX}:{signature}:"
        attempts = await self.db.list_tasks_by_dedup_prefix(project_id, prefix)
        live = [task for task in attempts if task.status not in _SPENT_STATUSES]
        spent = [task for task in attempts if task.status in _SPENT_STATUSES]
        if live:
            attempt = len(attempts)
            dedup_key = live[0].dedup_key or f"{prefix}{attempt}"
            escalated = False
        else:
            attempt = len(attempts) + 1
            dedup_key = f"{prefix}{attempt}"
            escalated = len(spent) >= max_attempts
        title, description = render_repair_task(
            ref=ref,
            head_sha=head_sha,
            failing_checks=list(verdict.failing),
            failing_tests=tests_sorted,
            run_url=run_url,
            attempt=attempt,
        )
        result.update(
            {
                "failing_tests": tests_sorted,
                "run_url": run_url,
                "signature": signature,
                "attempt": attempt,
                "prior_attempts": [task.id for task in spent],
                "escalated": escalated,
                "dedup_key": dedup_key,
                "title": title,
                "description": description,
                "escalation_key": f"{ESCALATION_KEY_PREFIX}:{signature}",
                "escalation_title": f"CI on {ref} still red after {len(spent)} repair attempt(s)",
                "escalation_question": (
                    f"`{ref}` is red at {head_sha or 'an unknown commit'} with failure signature "
                    f"{signature} ({', '.join(verdict.failing) or 'no named checks'}). "
                    f"Repair attempts {', '.join(task.id for task in spent) or 'none'} did not "
                    "make it green. Decide: fix it by hand, retarget the repair, or accept the "
                    "red state; resolve this gate when done."
                ),
            }
        )
        return result
