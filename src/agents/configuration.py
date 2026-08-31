"""Apply individual worker settings without changing reusable profiles."""
from __future__ import annotations

from copy import copy
from dataclasses import is_dataclass, replace

from sqlalchemy.exc import IntegrityError

from src.models import Agent
from src.sessions.spec import _infer_provider_from_harness

SUPERVISOR_AGENT_ID = "supervisor-global"


def apply_agent_overrides(profile, agent, *, agent_profile=None):
    """Copy capabilities and apply the worker's saved or inherited launch choices."""
    if profile is None or agent is None:
        return profile
    effective = replace(profile) if is_dataclass(profile) else copy(profile)
    inherited_harness = getattr(agent_profile, "harness", None)
    harness = getattr(agent, "harness", None) or inherited_harness
    model = getattr(agent, "model", None)
    inherited_model = getattr(agent_profile, "model", None)
    class_id = (
        getattr(agent, "intelligence_class", None)
        or getattr(agent_profile, "default_class", None)
    )
    if harness:
        if harness != getattr(profile, "harness", None) and not model:
            # A model name from one CLI is not a valid default for another.
            effective.model = ""
        effective.harness = harness
    if inherited_model and (not inherited_harness or harness == inherited_harness):
        # Profile models remain fallbacks beneath the selected class; only an
        # explicit worker model is fixed above class resolution.
        effective.model = inherited_model
    if model:
        effective.model = model
        effective._agent_model_override = model
    if class_id:
        effective.default_class = class_id
        effective._agent_intelligence_class = class_id
    return effective


def resolve_launch_settings(profile, harness, builder, task_class=None) -> dict:
    """The LLM settings actually used by the shared SessionSpecBuilder."""
    provider = getattr(harness, "provider", "") or _infer_provider_from_harness(harness)
    class_id = (
        getattr(profile, "_agent_intelligence_class", None)
        or task_class
        or getattr(profile, "default_class", None)
        or None
    )
    model = builder._resolve_model(profile, harness, task_class)
    return {
        "llm_provider": provider or None,
        "model": model or None,
        "intelligence_class": class_id,
    }


async def ensure_supervisor_agent(db) -> Agent:
    """Idempotently register the existing global supervisor as one worker."""
    existing = await db.get_agent(SUPERVISOR_AGENT_ID)
    if existing is not None:
        if getattr(existing, "role", "worker") != "supervisor":
            raise ValueError("supervisor-global is reserved for the global supervisor")
        return existing
    agent = Agent(
        id=SUPERVISOR_AGENT_ID, name="Supervisor", profile_id="supervisor", role="supervisor",
    )
    try:
        await db.create_agent(agent)
    except IntegrityError:
        # Startup and an on-demand wake can both initialize the registry.
        existing = await db.get_agent(SUPERVISOR_AGENT_ID)
        if existing is None or getattr(existing, "role", "worker") != "supervisor":
            raise
        return existing
    return agent
