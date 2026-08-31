"""Pure compatibility checks for assigning tasks to durable global workers.

Task profiles supply capabilities; a worker's own profile and saved overrides
supply its execution identity. Routing requirements are constraints, unlike
advisory agent affinity: waiting never permits a different provider or tier.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

from src.intelligence_classes import resolve_class
from src.sessions.spec import _infer_provider_from_harness, _is_codex_cli


_GENERIC_WORKER_PROFILES = frozenset({"worker-deep", "worker-standard", "worker-fast"})
_CLASS_CAPABILITY_PROFILES = _GENERIC_WORKER_PROFILES | {
    "triage", "reviewer", "final-reviewer", "playbook-compiler", "spec-ingest",
}


def _value(source, name: str) -> str:
    return str(getattr(source, name, None) or "").strip()


def resolve_profile(profiles: Mapping, profile_id: str | None, project_id: str | None = None):
    """Resolve a profile from a snapshot with the normal project override."""
    if not profile_id:
        return None
    if project_id:
        scoped = profiles.get(f"project:{project_id}:{profile_id}")
        if scoped is not None:
            return scoped
    return profiles.get(profile_id)


def resolve_task_profile(task, project, profiles: Mapping):
    """Task/project requirements only; never substitute the candidate worker."""
    return resolve_profile(
        profiles, _value(task, "profile_id") or _value(project, "default_profile_id"),
        _value(task, "project_id") or None,
    )


def resolve_agent_profile(agent, project_id: str | None, profiles: Mapping):
    return resolve_profile(profiles, _value(agent, "profile_id"), project_id)


def _generic_worker_profile(profile) -> bool:
    # Tags are deliberately not used: a provider-specific copy can retain
    # "generic" tags from its source. Bundled class-backed capabilities use
    # provider mappings; an explicit copied/custom harness stays binding.
    profile_id = _value(profile, "id")
    return (
        profile_id in _CLASS_CAPABILITY_PROFILES
        and _value(profile, "harness") == "claude"
        and (
            bool(_value(profile, "default_class"))
            or (profile_id in _GENERIC_WORKER_PROFILES and not _value(profile, "model"))
        )
    )


def _harness(harness_id: str, project_id: str, registry):
    found = registry.get(harness_id, project_id) if registry is not None else None
    return found or SimpleNamespace(id=harness_id, command=harness_id, provider="")


def _class_model(class_id: str, harness, classes: Mapping | None) -> str:
    if classes is None or not class_id:
        return ""
    cls = classes.get(class_id)
    if cls is None:
        return ""
    provider = _value(harness, "provider") or _infer_provider_from_harness(harness)
    config = resolve_class(cls, "codex") if provider == "openai" and _is_codex_cli(harness) else {}
    if not config:
        config = resolve_class(cls, provider)
    return str(config.get("model") or "").strip()


def task_agent_mismatch(
    task,
    agent,
    *,
    task_profile=None,
    agent_profile=None,
    harness_registry=None,
    intelligence_classes: Mapping | None = None,
) -> str | None:
    """Return why this worker cannot fulfill the task's execution requirements.

    Both profiles must be the unmodified resolved definitions. This never
    changes a worker or a profile and performs no I/O. Availability, project
    capacity, and gates are separate checks performed by the caller.

    An explicit non-generic task profile or a classified/model-specific
    project default binds its harness/model. A task class (or task/project
    profile default class) binds the class and its provider
    model mapping. Saved worker classes match exactly: editable class names
    have no universal ordering and may represent cost as well as capability.
    Workers with no fixed class/model may inherit the requested task settings.

    None for intelligence_classes means a legacy caller has no class snapshot;
    an explicit empty snapshot instead reports missing requested classes.
    """
    required_class = _value(task, "intelligence_class") or _value(task_profile, "default_class")
    worker_class = _value(agent, "intelligence_class") or _value(agent_profile, "default_class")
    if required_class and worker_class and required_class != worker_class:
        return (
            f"requires intelligence class '{required_class}'; worker '{agent.id}' "
            f"is configured for '{worker_class}'"
        )

    # A classified/model-specific project default is also an execution
    # requirement. Only an unclassified harness-only legacy default remains
    # advisory, allowing the global worker's saved identity to supply it.
    provider_bound = (
        task_profile is not None
        and not _generic_worker_profile(task_profile)
        and bool(_value(task, "profile_id") or required_class or _value(task_profile, "model"))
    )
    required_harness = _value(task_profile, "harness") if provider_bound else ""
    worker_harness = (
        _value(agent, "harness") or _value(agent_profile, "harness")
        or _value(task_profile, "harness")
    )
    if required_harness and worker_harness != required_harness:
        return (
            f"requires harness '{required_harness}'; worker '{agent.id}' "
            f"uses '{worker_harness or 'unspecified'}'"
        )

    required_model = _value(task_profile, "model") if provider_bound else ""
    if not required_class and not required_model:
        return None
    harness = _harness(worker_harness, _value(task, "project_id"), harness_registry)
    class_model = _class_model(required_class, harness, intelligence_classes)
    if required_class and intelligence_classes is not None:
        if required_class not in intelligence_classes:
            return f"required intelligence class '{required_class}' is not configured"
        if not class_model:
            return (
                f"required intelligence class '{required_class}' has no model "
                f"for harness '{worker_harness or 'unspecified'}'"
            )
    # A profile model is a fallback. The selected class is authoritative,
    # including when a capability template still carries an older model name.
    required_model = class_model or required_model
    # Changing harness drops a model fallback belonging to the old CLI,
    # just as apply_agent_overrides does before building the actual session.
    inherited_model = _value(agent_profile, "model")
    inherited_harness = _value(agent_profile, "harness")
    if inherited_harness and inherited_harness != worker_harness:
        inherited_model = ""
    # Class selection wins over a profile's fallback model, but a saved
    # per-worker model is fixed (the same ordering as SessionSpecBuilder).
    worker_model = (
        _value(agent, "model")
        or _class_model(worker_class or required_class, harness, intelligence_classes)
        or inherited_model
        or _value(task_profile, "model")
        or required_model
    )
    if required_model and worker_model != required_model:
        return (
            f"requires model '{required_model}'; worker '{agent.id}' "
            f"uses '{worker_model or 'unspecified'}'"
        )
    return None
