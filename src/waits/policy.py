"""Activation controls new writers; persisted work keeps its recovery owner."""


def effective_policy(config, project_id=None):
    policy = getattr(config, "wait_policy", None)
    if project_id and callable(getattr(policy, "resolve_project", None)):
        return policy.resolve_project(project_id)
    return policy


def enabled(config, project_id=None, *, scheduler=False):
    policy = effective_policy(config, project_id)
    active = (getattr(policy, "v2_enabled", False) is True
              and (not scheduler or getattr(policy, "scheduler_waits_enabled", False) is True))
    if active or project_id is not None:
        return active
    return any(enabled(config, pid, scheduler=scheduler)
               for pid in getattr(policy, "projects", {}))
