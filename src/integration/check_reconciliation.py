"""CI-owner recovery for authenticated Actions checks that never concluded."""

from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import select, update

from src.database.tables import integration_batches, integration_check_evidence, tasks
from src.git.github import GitHubAccessError
from src.git.manager import GitError
from src.integration.row_query import select_row

STUCK_CHECK_SECONDS = 300.0


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except ValueError:
        return None


async def stuck_check_diagnostics(client, trust, sha, checks, workflows, *, expected_event):
    """Keep diagnostics separate from receipts; steps can never establish green."""
    from src.integration.ci import (
        _hosted_listing,
        _producer_matches,
        _require_workflow_repository,
        _strict_int,
    )

    result, job_listings = [], {}
    allowed_suites = {r.get("check_suite_id") for r in workflows
                      if r.get("head_sha") == sha
                      and r.get("event") == (expected_event or "push")}
    for name in trust.required_checks.names:
        matches = [c for c in checks if c.get("name") == name
                   and isinstance(c.get("app"), dict) and _producer_matches(c["app"], trust)
                   and isinstance(c.get("check_suite"), dict)
                   and c["check_suite"].get("id") in allowed_suites
                   and (_strict_int(c.get("id")) or 0) > 0]
        if not matches:
            continue
        check = max(matches, key=lambda c: c["id"])
        if (check.get("head_sha") != sha or check.get("status") != "in_progress"
                or check.get("conclusion") is not None):
            continue
        suite = check.get("check_suite")
        suite_id = _strict_int(suite.get("id")) if isinstance(suite, dict) else None
        runs = [r for r in workflows if suite_id and r.get("check_suite_id") == suite_id
                and (_strict_int(r.get("id")) or 0) > 0
                and (_strict_int(r.get("run_attempt")) or 0) > 0]
        if not runs:
            continue
        run = max(runs, key=lambda r: (r["run_attempt"], r["id"]))
        if (run.get("head_sha") != sha
                or run.get("event") != (expected_event or "push")):
            continue
        _require_workflow_repository(run, trust)
        attempt = (run["id"], run["run_attempt"])
        if attempt not in job_listings:
            job_listings[attempt] = await _hosted_listing(client,
                f"/repos/{trust.full_name}/actions/runs/{run['id']}"
                f"/attempts/{run['run_attempt']}/jobs?per_page=100", key="jobs")
        jobs = job_listings[attempt]
        matching = [j for j in jobs if j.get("name") == name]
        if len(matching) != 1:
            continue
        job = matching[0]
        if (_strict_int(job.get("run_id")) != run["id"]
                or _strict_int(job.get("run_attempt")) != run["run_attempt"]
                or job.get("head_sha") != sha or (_strict_int(job.get("id")) or 0) <= 0
                or job.get("check_run_url") !=
                f"https://api.github.com/repos/{trust.full_name}/check-runs/{check['id']}"):
            continue
        completed = []
        if (run.get("status") == "completed"
                and (stamp := _timestamp(run.get("updated_at"))) is not None):
            completed.append(stamp)
        steps = job.get("steps")
        if (isinstance(steps, list) and steps
                and all(isinstance(s, dict) and s.get("status") == "completed"
                        and _timestamp(s.get("completed_at")) is not None for s in steps)):
            completed.append(max(_timestamp(s["completed_at"]) for s in steps))
        if not completed:
            continue
        result.append({
            "name": name, "head_sha": sha, "check_run_id": check["id"],
            "check_suite_id": suite_id, "workflow_run_id": run["id"],
            "run_attempt": run["run_attempt"], "job_id": job["id"],
            "completed_at": min(completed), "workflow_status": run.get("status"),
            "job_url": f"https://github.com/{trust.full_name}/actions/runs/"
                       f"{run['id']}/job/{job['id']}",
        })
    return result


class CheckReconciliation:
    """Reserve once before I/O, including failures whose remote result is ambiguous."""

    def __init__(self, db, *, clock=time.time, grace_seconds=STUCK_CHECK_SECONDS):
        self.db, self.clock, self.grace_seconds = db, clock, grace_seconds

    async def run(self, batch, head, checks, *, authorize, names=None, dry_run=False,
                  actor="ci-owner", reason="stuck_required_check"):
        from src.integration.checks import Conclusion, HostedChecks, lock_check_evidence
        from src.integration.repair import ordinary_repair_id
        from src.operator_decisions import history_on, related_on

        provider = checks.provider
        if not isinstance(provider, HostedChecks) or head.repository_id != batch.repository_id:
            return []
        cached = await checks.read(head)
        diagnostics = {
            d["name"]: d for row in cached.checks if row.conclusion is Conclusion.PENDING
            for d in row.detail.get("stuck_checks", ())
            if d["name"] in checks.required.names and d.get("head_sha") == head.sha
            and (names is None or d["name"] in names)
        }
        table = integration_check_evidence
        results = []
        for name, diagnostic in diagnostics.items():
            key = (table.c.repository_id == head.repository_id, table.c.sha == head.sha,
                   table.c.producer_id == checks.required.producer_id, table.c.check_name == name)
            now = self.clock()
            evidence = {**diagnostic, "actor": actor, "reason": reason, "batch_id": batch.id,
                        "sha": head.sha, "required_check_version": checks.required.version}
            if not await authorize():
                results.append({**evidence, "outcome": "refused", "reason": "candidate_moved"})
                continue
            async with self.db.immediate() as conn:
                await lock_check_evidence(conn, head.repository_id, head.sha)
                current = await select_row(conn, integration_batches,
                                           integration_batches.c.id == batch.id, lock=True)
                if (current is None or current["intent"] != "open"
                        or current["repository_id"] != head.repository_id
                        or current["project_id"] != batch.project_id
                        or current["target_ref"] != batch.target_ref
                        or current["lifecycle"] not in {"sealed", "building", "testing"}
                        or current["repair_attempt_count"] != batch.repair_attempt_count):
                    results.append({**evidence, "outcome": "refused", "reason": "batch_changed"})
                    continue
                repair = await conn.scalar(select(tasks.c.id).where(
                    tasks.c.id == ordinary_repair_id(batch.id, batch.repair_attempt_count),
                    tasks.c.status.not_in(("COMPLETED", "FAILED", "CANCELLED")),
                )) if batch.repair_attempt_count else None
                if repair:
                    results.append({**evidence, "outcome": "refused", "reason": "repair_open"})
                    continue
                holds = await history_on(conn, batch.project_id,
                                         await related_on(conn, "batch", batch.id))
                if any(h["active"] for h in holds):
                    results.append({**evidence, "outcome": "refused", "reason": "operator_hold"})
                    continue
                cached_row = await select_row(conn, table, *key, lock=True)
                if (cached_row is None or cached_row["conclusion"] != Conclusion.PENDING
                        or diagnostic not in (cached_row["checks"] or {}).get("stuck_checks", ())):
                    continue
                previous = cached_row["check_reconciliation"]
                if previous:
                    results.append({**previous, "outcome": "already_attempted",
                                    "request_outcome": previous["outcome"]})
                    continue
                if now - diagnostic["completed_at"] < self.grace_seconds:
                    results.append({**evidence, "outcome": "waiting",
                                    "due_at": diagnostic["completed_at"] + self.grace_seconds})
                    continue
                if dry_run:
                    results.append({**evidence, "outcome": "preview"})
                    continue
                reservation = {**evidence, "outcome": "reserved", "created_at": now,
                               "updated_at": now}
                await conn.execute(update(table).where(*key).values(
                    check_reconciliation=reservation))
            outcome = "requested"
            try:
                await provider.producer.client.rerun_job(diagnostic["job_id"])
            except (GitHubAccessError, GitError, OSError, ValueError, TimeoutError) as exc:
                outcome = "unavailable"
                evidence["error"] = getattr(exc, "category", type(exc).__name__)
                if evidence["error"] == "permission":
                    evidence["classification"] = "permission_blocked"
                    evidence["remediation"] = (
                        "Operator: grant Actions write to the App installation and set "
                        "integration.github_app.actions_permission: write, then restart; "
                        "existing-login credentials also require Actions write. "
                        "This head/check has consumed its automatic request."
                    )
            async with self.db._engine.begin() as conn:
                await lock_check_evidence(conn, head.repository_id, head.sha)
                await conn.execute(update(table).where(*key).values(
                    check_reconciliation={**reservation, "outcome": outcome,
                                          **evidence, "updated_at": self.clock()}))
            results.append({**evidence, "outcome": outcome})
        return results
