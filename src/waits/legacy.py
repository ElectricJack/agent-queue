"""Pure v1 request translation; no row bridging or runtime activation."""

from src.waits.conditions import WaitError


def translate_registration(values):
    kind = values.kind
    if kind == "timer":
        condition = {"kind": kind, "due_at": values.due_at}
    elif kind == "message":
        condition = {"kind": kind, "thread_id": values.ref, "after_seq": values.after_seq}
    elif kind in {"job", "task"}:
        condition = {"kind": kind, f"{kind}_id": values.ref}
    else:
        raise WaitError("wait.condition_required", "a typed condition is required")
    return condition
