"""Inspect configured routing policy before a new task can be dispatched.

Pipeline callbacks run asynchronously. Their routing gates must be committed
with task creation, not attached later after a worker could have claimed it.
Cooldown and runner capacity delay execution; they do not waive routing.
"""

from dataclasses import asdict, is_dataclass

from src.event_bus import EventBus
from src.playbooks.conditions import eval_pipeline_when


def _selected_pipelines(manager, event, *, match_filter=True):
    if manager is None:
        return []
    if (
        getattr(getattr(getattr(manager, "_config", None), "playbooks", None), "enabled", True)
        is False
    ):
        return []
    candidates = manager.get_playbooks_by_trigger("task.created")
    # Lightweight handlers may not have a configured playbook registry yet.
    if not isinstance(candidates, list):
        return []
    selected = manager._select_after_shadowing(candidates, event)
    return [
        pb
        for pb in selected
        if pb.kind == "pipeline"
        and pb.enabled
        and manager._matches_scope(pb, event)
        and (
            not match_filter
            or any(
                trigger.event_type == "task.created"
                and EventBus._matches_filter(event, trigger.filter)
                for trigger in pb.triggers
            )
        )
    ]


def _rule_actions(pb, event, *, check_when=True):
    graph = pb.to_dict()
    rules = graph.get("pipeline_rules", {}).get("task.created")
    nodes = graph.get("nodes", {})
    if rules is None:
        rules = [{"entry": key} for key, node in nodes.items() if node.get("entry")]
    elif isinstance(rules, (str, dict)):
        rules = [rules]
    rules = [{"entry": rule} if isinstance(rule, str) else rule for rule in rules]
    visited = set()
    pending = [
        rule["entry"]
        for rule in rules
        if not check_when or eval_pipeline_when(rule.get("when", {}), event)
    ]
    while pending:
        key = pending.pop()
        if key in visited:
            continue
        visited.add(key)
        node = nodes.get(key, {})
        action = node.get("action")
        if not isinstance(action, dict):
            continue
        yield action
        pending.extend(action[edge] for edge in ("on_success", "on_failure") if action.get(edge))


def requires_routing_gate(manager, task, event_extra=None) -> bool:
    """Whether a selected task-created rule attaches a routing gate to this task."""
    row = asdict(task) if is_dataclass(task) else dict(task)
    if event_extra and "parent_task_id" in event_extra:
        row["parent_task_id"] = event_extra["parent_task_id"]
    if (row.get("profile_id") or "").strip():
        return False
    event = {**row, "task_id": row.get("id"), "task": row, **(event_extra or {})}
    for pb in _selected_pipelines(manager, event):
        for action in _rule_actions(pb, event):
            args = action.get("args") or {}
            waiters = args.get("waiter_task_ids") or []
            if isinstance(waiters, str):
                waiters = [waiters]
            if (
                action.get("command") == "gate_create"
                and args.get("gate_type") == "routing"
                and any(
                    str(w).replace(" ", "") in ("{{event.task_id}}", "{{event.task.id}}")
                    for w in waiters
                )
                and args.get("project_id") in ("{{event.project_id}}", row.get("project_id"))
            ):
                return True
    return False


def uses_default_triage(manager, project_id: str) -> bool:
    """Whether the selected routing pipeline uses the reusable project triage job.

    Recovery has gates, not the original event. Inspect rule capability without
    reapplying event-only filters; project scope and shadowing still apply.
    """
    event = {"project_id": project_id}
    return any(
        action.get("command") == "ensure_task"
        and (action.get("args") or {}).get("profile_id") == "triage"
        and (action.get("args") or {}).get("dedup_key") == "triage-open"
        for pb in _selected_pipelines(manager, event, match_filter=False)
        for action in _rule_actions(pb, event, check_when=False)
    )
