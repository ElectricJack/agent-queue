"""Read-only counterfactual routing over exported historical evidence.

Run ``python -m src.routing.replay --help``. No daemon or database access;
missing capacity observations are reported rather than invented.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from src.routing.planner import ProfileFacts, ProviderFacts, Snapshot, TaskFacts, plan_route
from src.routing.policy import RoutingPolicy, parse_policy


def replay_routes(records: list[dict], policy: RoutingPolicy, digest: str) -> dict:
    before, after, limitations = Counter(), Counter(), Counter()
    results = []
    for record in records:
        route = record.get("route") or {}
        task_id = record.get("task_id") or record.get("id")
        if record.get("route_source", "router") != "router" or route.get("benchmark_arm"):
            results.append({"task_id": task_id, "skipped": "operator_role_or_benchmark_route"})
            continue
        candidates = route.get("candidates") or []
        scores = {s["profile_id"]: s for s in route.get("scores") or []}
        context = route.get("live_context") or {}
        observed = {p["profile_id"]: p for p in context.get("profiles", [])}
        providers = {}
        for p in context.get("providers", []):
            providers[p["provider"]] = ProviderFacts(
                state=p["state"], launchable=p["launchable"], quota=tuple(p.get("quota") or ()),
                reason_code=p.get("reason", ""), updated_at=p.get("updated_at"),
            )
        profiles, busy, backlog, headroom = [], {}, {}, {}
        incomplete = False
        for c in candidates:
            s, p = scores.get(c["profile_id"]), observed.get(c["profile_id"])
            if s is None and p is None:
                incomplete = True
                break
            slots = p["limit"] if p else s["slots"]
            profiles.append(ProfileFacts(
                id=c["profile_id"], harness=c["harness"], provider=c["provider"],
                lifecycle=c["lifecycle"], default_class=c["intelligence_class"],
                classes=frozenset({c["intelligence_class"]}), slots=slots,
                enabled=p.get("enabled", True) if p else True,
            ))
            busy[c["profile_id"]] = s["busy"] if s else p["busy"]
            backlog[c["profile_id"]] = s["backlog"] if s else p["routed_backlog"]
            if p is not None:
                headroom[c["profile_id"]] = p["effective_headroom"]
            if c["provider"] not in providers and s is not None:
                providers[c["provider"]] = ProviderFacts(
                    state="degraded" if s["avail_factor"] < 1 else "available",
                    usage_percent=s.get("usage_percent"),
                )
        if incomplete or not profiles or not route.get("provider"):
            results.append({"task_id": task_id, "skipped": "incomplete_candidate_evidence"})
            continue
        missing = []
        if not context:
            missing.append("headroom_and_snapshot_age_unknown")
        if any(not p.quota for p in providers.values()):
            missing.append("quota_window_identity_or_age_unknown")
        constraints = route.get("constraints") or {}
        hints = route.get("hints") or {}
        # The rule names the origin the router routed on, which for a repair is
        # projected from its stored ``created_by_kind`` (``system``,
        # ``source_ci_repair``); the stored origin answers only for a rule
        # that matched none.
        origin = record.get("created_by_kind")
        if "+origins." in route.get("rule", ""):
            origin = route["rule"].split("+origins.", 1)[1]
        result = plan_route(TaskFacts(
            task_id=task_id, task_type=route.get("task_type") or record.get("task_type"),
            class_hint=hints.get("class_hint"), created_by_kind=origin,
            exclude_providers=frozenset(constraints.get("exclude_providers") or ()),
            preferred_provider=record.get("preferred_provider"),
            needs_task_lifecycle=bool(record.get("needs_task_lifecycle")),
        ), policy, Snapshot(tuple(profiles), providers, busy, backlog, context, headroom),
            policy_sha256=digest, classification=route.get("classification") or {"failed": True})
        selected = result.value.get("provider") or result.outcome
        before[route["provider"]] += 1
        after[selected] += 1
        limitations.update(missing)
        results.append({
            "task_id": task_id, "before": route["provider"], "after": selected,
            "changed": selected != route["provider"], "outcome": result.outcome,
            "profile_id": result.value.get("profile_id"), "reason": result.value.get("reason"),
            "missing_observations": missing,
        })
    return {
        "version": 1, "policy_sha256": digest, "records": len(records),
        "replayed": sum(before.values()), "skipped": len(records) - sum(before.values()),
        "before": dict(before), "after": dict(after),
        "changed": sum(bool(r.get("changed")) for r in results),
        "missing_observations": dict(limitations), "results": results,
        "limitations": [
            "Recorded candidate cells only; no hypothetical capacity or newly installed profiles.",
            "Independent historical observations, not a simulated queue or measured quota savings.",
            "Unknown headroom falls back to recorded load/slots; do not treat it as observed spare capacity.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="JSON array of task/route records")
    parser.add_argument("--policy-source", type=Path, required=True, help="Reviewed router source.md")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = args.input.read_bytes()
    source = args.policy_source.read_text()
    policy, digest = parse_policy(source.split("```yaml\n", 1)[1].split("```", 1)[0])
    report = replay_routes(json.loads(raw), policy, digest)
    report["input_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
    report["source_sha256"] = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({k: report[k] for k in ("replayed", "skipped", "before", "after", "changed")}))


if __name__ == "__main__":
    main()
