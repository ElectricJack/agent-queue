"""Exact source CI facts; task creation is delegated to CommandHandler."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.integration.models import HierarchicalIntegrationPolicy


@dataclass(frozen=True)
class SourceCIObservation:
    task_id: str
    source: dict[str, Any]
    policy_generation: int
    state: str
    evidence: dict[str, Any]


def classify_source_checks(entries, *, head, required):
    """Judge the latest trusted run of each required check, including cancellation.

    A cancelled old run cannot supersede a newer pending/successful rerun.
    Missing, foreign-producer and mismatched-head checks never establish green.
    """
    latest = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        app = entry.get("app") or {}
        name = entry.get("name")
        if (
            name not in required.names
            or entry.get("head_sha") != head
            or str(app.get("id")) != required.producer_id
        ):
            continue
        identity = (
            str(entry.get("started_at") or entry.get("created_at") or ""),
            int(entry.get("id") or 0),
        )
        if name not in latest or identity > latest[name][0]:
            latest[name] = (identity, entry)
    checks = [item[1] for item in latest.values()]
    failed = [
        item
        for item in checks
        if item.get("status") == "completed"
        and item.get("conclusion") in {"failure", "timed_out", "action_required"}
    ]
    cancelled = [
        item
        for item in checks
        if item.get("status") == "completed" and item.get("conclusion") == "cancelled"
    ]
    pending = any(item.get("status") != "completed" for item in checks)
    if failed:
        state = "red"
    elif cancelled and not pending:
        state = "cancelled"
    elif len(latest) == len(required.names) and all(
        item.get("status") == "completed" and item.get("conclusion") == "success" for item in checks
    ):
        state = "green"
    else:
        state = "pending"
    evidence = {
        "head_sha": head,
        "producer_id": required.producer_id,
        "required_checks_version": required.version,
        "required_checks": list(required.names),
        "checks": [
            {
                "id": item.get("id"),
                "name": item["name"],
                "status": item.get("status"),
                "conclusion": item.get("conclusion"),
                "url": item.get("html_url") or item.get("details_url"),
                "summary": str((item.get("output") or {}).get("summary") or "")[:4000],
            }
            for item in sorted(checks, key=lambda item: item["name"])
        ],
        "failing_checks": [item["name"] for item in failed + cancelled],
    }
    return state, evidence


async def observe_source_ci(*, row, source, client, handler):
    policy_data = row.get("hierarchical_integration_policy")
    if not policy_data:
        return
    policy = HierarchicalIntegrationPolicy.model_validate(policy_data)
    if not policy.root.repair.source_ci:
        return
    state, evidence = classify_source_checks(
        await client.commit_check_runs(source["head"]),
        head=source["head"],
        required=policy.root.required_checks,
    )
    await handler(
        SourceCIObservation(
            task_id=row["id"],
            source=source,
            policy_generation=row["hierarchical_integration_generation"],
            state=state,
            evidence=evidence,
        )
    )


def repair_description(observation):
    source, evidence = observation.source, observation.evidence
    checks = "\n".join(
        f"- {item['name']}: {item['conclusion']} {item.get('url') or ''}\n"
        f"  {item.get('summary') or ''}"
        for item in evidence["checks"]
        if item["name"] in evidence["failing_checks"]
    )
    return (
        f"Authorized source CI repair for task {observation.task_id}, PR {source['pr_url']}.\n"
        f"Repository {source['repository_id']}; source branch {source['branch']}; "
        f"exact source base {source['base']}, head {source['head']}, "
        f"checkpoint generation {source['generation']}.\n"
        f"Observed {observation.state} under policy generation {observation.policy_generation}.\n"
        f"Failing/cancelled checks:\n{checks}\n\n"
        "Work in your assigned root branch. Fetch and merge the exact source head "
        "above, preserving it as an ancestor; resolve all necessary code, test, "
        "migration and generated-artifact issues. Use the failed check links and "
        "record concrete failures and fixes. A cancelled check requires investigation "
        "and a fresh complete run, not fabricated success. Re-chain colliding migration "
        "revisions and regenerate generated files. Run focused and relevant area checks "
        "with aq test, publish your assigned branch and close with exact test evidence. "
        "The delivery service observes this branch's own exact CI and queues further "
        "bounded repair if needed; only its fully checked candidate can reach main. "
        "Do not rewrite the source branch or publish main."
    )
