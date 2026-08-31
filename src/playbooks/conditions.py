"""Condition evaluation shared by pipeline dispatch and task admission."""

import logging

logger = logging.getLogger(__name__)


def eval_pipeline_when(when: dict, event: dict) -> bool:
    """Evaluate a simple pipeline rule ``when`` condition against the event.

    Supported shapes::

        {"field": "event.task.branch_name", "truthy": true}
            — pass when the dot-path resolves to a non-empty truthy value.

        {"field": "event.task.branch_name", "not_null": true}
            — pass when the dot-path resolves to a non-None / non-empty value.

        {"field": "event.created_by_kind", "equals": "session"}
            — pass when the dot-path resolves to a value ``== `` the given
            comparator value (any type, compared with ``==``).

        {"field": "event.parent_task_id", "is_null": true}
            — pass when the dot-path resolving to ``None`` matches the
            given boolean (``is_null: false`` passes when non-None).

        {"all": [<clause>, <clause>, ...]}
            — pass when every nested clause passes (AND).

        {"any": [<clause>, <clause>, ...]}
            — pass when at least one nested clause passes (OR).

    Unrecognised shapes default to *True* (permissive: unknown conditions do
    not silently drop events).
    """
    if not isinstance(when, dict):
        return True

    if "all" in when:
        clauses = when.get("all") or []
        if not isinstance(clauses, list):
            return True
        if not clauses:
            # Vacuous ``all: []`` returns True and silently fires on every
            # event — almost certainly an author bug. Compile-time
            # validation rejects this shape; log if it slips through.
            logger.warning("pipeline when.all is empty — vacuous True; reject at compile time")
            return True
        return all(eval_pipeline_when(clause, event) for clause in clauses)

    if "any" in when:
        clauses = when.get("any") or []
        if not isinstance(clauses, list):
            return True
        if not clauses:
            # Empty ``any: []`` returns False and silently disables the
            # rule. Compile-time validation rejects this shape.
            logger.warning("pipeline when.any is empty — silently False; reject at compile time")
            return False
        return any(eval_pipeline_when(clause, event) for clause in clauses)

    field_path = when.get("field", "")
    if not field_path:
        return True

    # Walk the dot-path into the event dict
    val: object = event
    for index, part in enumerate(field_path.split(".")):
        if index == 0 and part == "event":
            # "event.foo" means the event itself as root — skip the prefix
            continue
        if isinstance(val, dict):
            val = val.get(part)
        else:
            val = None
            break

    if "truthy" in when:
        return bool(val) is bool(when["truthy"])
    if "not_null" in when:
        return (val is not None and val != "") is bool(when["not_null"])

    if "equals" in when:
        return val == when["equals"]
    if "is_null" in when:
        return (val is None) == bool(when["is_null"])

    # Unknown condition shape — permissive default
    return True
