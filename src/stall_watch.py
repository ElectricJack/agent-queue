"""Read-only stuck-task detection and deterministic supervisor summary formatting."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from types import SimpleNamespace

from src.agents.liveness import is_live, liveness_by_agent

ORPHAN_AFTER = 20 * 60
READY_AFTER = 30 * 60
ROUTE_CODES = {"route_no_candidates", "frontier_route_not_claimable"}


@dataclass
class Snapshot:
    tasks: dict[str, dict] = field(default_factory=dict)
    containers: set[str] = field(default_factory=set)
    held: set[str] = field(default_factory=set)
    agents: dict[str, dict] = field(default_factory=dict)
    sessions: list[dict] = field(default_factory=list)
    reasons: dict[str, list[dict]] = field(default_factory=dict)


def apply_holds(snapshot: Snapshot) -> None:
    """A held ancestor also withholds its descendants."""
    for tid, reasons in snapshot.reasons.items():
        if any(r["code"] in {"held", "operator_decision_hold"} or (
            r["code"] == "blocked_gate" and "(human:" in r.get("detail", "")
        ) for r in reasons):
            snapshot.held.add(tid)
    while True:
        inherited = {t["id"] for t in snapshot.tasks.values()
                     if t.get("parent_task_id") in snapshot.held} - snapshot.held
        if not inherited:
            return
        snapshot.held.update(inherited)


def detect(snapshot: Snapshot, now: float, *, assigned: int, in_progress: int) -> list[dict]:
    apply_holds(snapshot)
    findings = []
    for task in sorted(snapshot.tasks.values(), key=lambda t: (t["updated_at"], t["id"])):
        tid, status = task["id"], task["status"]
        if tid in snapshot.held or tid in snapshot.containers:
            continue
        age = now - task["updated_at"]
        reason = None
        kind = None
        if status in {"ASSIGNED", "IN_PROGRESS"}:
            agent_id = task.get("assigned_agent_id")
            agent = snapshot.agents.get(agent_id, {})
            # Check the session holding this task/epoch, not an idle worker's
            # newer session or an old attempt that still says 'running'.
            sessions = [SimpleNamespace(**s) for s in snapshot.sessions
                        if s.get("task_id") == tid and s.get("agent_id") == agent_id
                        and s.get("lifecycle") != "named" and not s.get("ended_at")
                        and (s.get("lifecycle") != "pool"
                             or s.get("last_claim_epoch") == task.get("claim_epoch"))]
            activity = liveness_by_agent(sessions).get(agent_id)
            running = (agent.get("state") == "BUSY" and agent.get("current_task_id") == tid
                       and is_live(activity, now=now, ttl=ORPHAN_AFTER)
                       and is_live(agent.get("last_heartbeat"), now=now, ttl=ORPHAN_AFTER))
            if age > ORPHAN_AFTER and not running:
                kind = "ORPHANED"
                reason = "Assigned worker is idle, has no live task session, or its heartbeat is >20m old"
            elif age > (assigned if status == "ASSIGNED" else in_progress):
                kind = "STALE ACTIVE"
                reason = "Task exceeded its configured time-in-state threshold"
        elif status == "READY" and age > READY_AFTER:
            from src.task_graph.readiness import DELIVERY_CODES, readiness_from_reasons

            all_reasons = snapshot.reasons.get(tid, [])
            reasons = [r for r in all_reasons if r["code"] in ROUTE_CODES]
            if any(r["code"] in DELIVERY_CODES for r in all_reasons):
                kind = "WAITING-ON-DELIVERY"
                reason = readiness_from_reasons(all_reasons)["reason"]
            elif reasons:
                kind = "STUCK READY"
                reason = "; ".join(f"{r['code']}: {r.get('detail', '')}" for r in reasons)
        if reason:
            findings.append({
                "id": tid, "project_id": task["project_id"], "status": status,
                "assigned_agent": task.get("assigned_agent_id"), "updated_at": task["updated_at"],
                "seconds_in_state": age, "kind": kind, "reason": reason,
                "suggested_command": f"aq task explain --task-id {shlex.quote(tid)}",
            })
    return findings


def format_summary(findings: list[dict]) -> str:
    """One line per task, suitable for the playbook's message_send body."""
    return "\n".join(
        f"{f['project_id']} {f['id']} [{f['status']}, {int(f['seconds_in_state'] // 60)}m] "
        f"{f['kind']}: {' '.join(f['reason'].splitlines())}; "
        f"suggested: {f['suggested_command']}"
        for f in findings
    )
