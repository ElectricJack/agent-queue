"""Exact candidate checks through existing managed jobs and trusted hosted CI."""
from __future__ import annotations

from src.integration.close_merge import policy_digest
from src.integration.models import RequiredCheckSet, integration_ci_policy
from src.jobs.adapters import PublisherJobs, finite_command
from src.jobs.policy import JobError, TERMINAL


async def check_candidate(runtime, row, repo, policy):
    project = await runtime.db.get_project(row["project_id"])
    raw = project.hierarchical_integration_policy or {}
    ci = integration_ci_policy(raw)
    required = policy.required_checks if policy.required_checks is not None else (
        (raw.get("parent") or {}).get("required_checks") or {})
    names = required.get("names") or ()
    source = ci.source_for("epic") if ci else "hosted"
    commands = list(row["close_args"].get("tests") or ())
    if source in {"local", "hybrid"}:
        if any(name not in ci.commands for name in names):
            return {"state": "unavailable", "reason": "required_local_command_unconfigured"}
        commands.extend(ci.commands[name] for name in names)
    commands = list(dict.fromkeys(commands))
    hosted = bool(names and (source == "hosted" or ci.requires_hosted("epic")))
    if not commands and not hosted:
        return {"state": "unavailable", "reason": "no_candidate_validation_command"}
    head = row["candidate_oid"]
    key = policy_digest([head, commands, required, source])
    records = []
    pending = None
    publisher = PublisherJobs(lambda: runtime.orch._command_handler)
    for ordinal, command in enumerate(commands):
        try:
            preset, argv = finite_command(command)
            job = await publisher.submit(
                project_id=row["project_id"], operation_id=row["id"],
                repository_id=row["repository_id"], store=str(repo.store), input_ref=head,
                preset=preset, argv=argv, queue_seconds=ci.queue_seconds if ci else 1800,
                run_seconds=ci.run_seconds if ci else 1800,
                idempotency_key=f"close:{row['id']}:{key}:{ordinal}", snapshot_group=key,
            )
        except JobError as exc:
            return {"state": "unavailable", "reason": str(exc)}
        records.append({"job_id": job["id"], "command": command, "state": job["state"],
                        "exit_code": job["exit_code"], "head_sha": head,
                        "result": job.get("result")})
        if job["state"] in TERMINAL:
            if job["state"] != "succeeded" or job["exit_code"] != 0:
                return {"state": "failed", "jobs": records, "check_key": key}
        elif pending is None:
            pending = {"kind": "job", "job_id": job["id"]}
    if pending:
        from src.waits.policy import enabled

        if not enabled(runtime.orch.config, row["project_id"]):
            return {"state": "unavailable", "jobs": records, "reason": "candidate_waits_disabled"}
        return {"state": "pending", "jobs": records, "check_key": key, "condition": pending}
    checks = None
    if hosted:
        from src.integration.completion_ci import CompletionChecks, observe_completion_ci

        remote = await runtime.gitops.remote(repo, row["candidate_ref"])
        if remote != head:
            async def authorize():
                await runtime.authorize(row)
                current = await runtime.policy(row)
                return current.enabled and current.digest == policy.digest
            await runtime.publish(repo, row["candidate_ref"], expected_old_oid=remote or "0" * 40,
                                  new_oid=head, authorize=authorize)
        checks = await observe_completion_ci(runtime.git, f"https://github.com/{repo.binding.full_name}.git", head,
            CompletionChecks(tuple(names), RequiredCheckSet.model_validate(required)))
        if checks["state"] != "green":
            from src.waits.policy import enabled

            if checks["state"] == "unknown":
                return {"state": "unavailable", "jobs": records, "hosted": checks,
                        "reason": "candidate_checks_unavailable"}
            pending_names = checks.get("pending_checks", [])
            if (checks.get("missing_checks") or pending_names) and (
                    not pending_names or not enabled(runtime.orch.config, row["project_id"])):
                return {"state": "unavailable", "jobs": records, "hosted": checks,
                        "reason": "candidate_check_identity_unavailable"}
            return {"state": "pending" if pending_names else "failed", "jobs": records,
                    "hosted": checks, "check_key": key,
                    **({"condition": {"kind": "ci_check", "repository_id": row["repository_id"],
                                      "head_sha": head, "check_name": pending_names[0]}}
                       if pending_names else {})}
    return {"state": "green", "jobs": records, "check_key": key, "head_sha": head,
            **({"hosted": checks} if checks else {})}
