"""Default worker pools for a fresh install.

Work reaches workers through pools: a pool worker claims the next ready task
itself (``aq task claim``).  Every derived worker profile is seeded as
``lifecycle: task`` and becomes a pool only when someone sizes it, so a fresh
install -- which never did -- had an active supervisor, active worker
profiles, and no pool for a single task to run on.

:func:`ensure_default_pools` closes that once, at the end of `aq install`: for
each agent CLI that is installed and signed in, its ``standard-high`` rung
becomes a pool that scales from zero to the machine's tuned concurrent-agent
limit.  ``min_active: 0`` means an idle install runs no workers and spends
nothing; ``standard-high`` is the class ordinary work is routed to, while the
heavier classes stay available for the routing playbook to pick deliberately.

It writes the rung's ``## Config`` in the vault -- the source of truth the
daemon's profile watcher syncs from -- so it works whether or not the daemon
is running.  It does nothing once *any* profile is already a pool: an operator
who has sized, removed or replaced the defaults keeps their choice.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The intelligence class a default pool serves.
DEFAULT_POOL_CLASS = "standard-high"
DEFAULT_MIN_ACTIVE = 0
#: Used only when the configuration names no concurrent-agent limit.
FALLBACK_MAX_ACTIVE = 2


@dataclass(frozen=True, slots=True)
class PoolDefaults:
    #: Rungs this run made into pools.
    created: tuple[str, ...] = ()
    #: Pools that were already configured, so nothing was changed.
    existing: tuple[str, ...] = ()
    max_active: int | None = None
    #: Why no pool exists after this run, when none does.
    problem: str | None = None

    @property
    def pools(self) -> tuple[str, ...]:
        return self.created + self.existing

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": list(self.created),
            "existing": list(self.existing),
            "max_active": self.max_active,
            "problem": self.problem,
        }


def _profiles_root(data_dir: str | Path) -> Path:
    return Path(data_dir) / "vault" / "agent-types"


def configured_pools(data_dir: str | Path) -> tuple[str, ...]:
    """The profile ids whose vault config already says ``lifecycle: pool``."""
    from src.profiles.parser import parse_profile

    pools = []
    for path in sorted(_profiles_root(data_dir).glob("*/profile.md")):
        try:
            config = parse_profile(path.read_text(encoding="utf-8")).config
        except (OSError, ValueError):
            continue
        if isinstance(config, Mapping) and config.get("lifecycle") == "pool":
            pools.append(path.parent.name)
    return tuple(pools)


def _max_active(config: Mapping[str, Any]) -> int:
    resources = config.get("resources")
    value = resources.get("max_concurrent_agents") if isinstance(resources, Mapping) else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return FALLBACK_MAX_ACTIVE


def ensure_default_pools(
    data_dir: str | Path,
    activations: Iterable[Any],
    config: Mapping[str, Any],
) -> PoolDefaults:
    """Give each signed-in agent CLI a ``standard-high`` pool, once."""
    from src.profiles.parser import update_config_keys

    existing = configured_pools(data_dir)
    if existing:
        return PoolDefaults(existing=existing)

    swarm = config.get("swarm")
    if not (isinstance(swarm, Mapping) and swarm.get("enabled") is True):
        return PoolDefaults(
            problem=(
                "swarm.enabled is not true in config.yaml, so a pool would never be started; "
                "set it with `aq system config edit`, then rerun `aq install`"
            )
        )

    chosen = sorted(
        {
            activation.profile.id
            for activation in activations
            if getattr(activation, "active", False)
            and activation.profile.intelligence_class == DEFAULT_POOL_CLASS
        }
    )
    max_active = _max_active(config)
    created = []
    for profile_id in chosen:
        path = _profiles_root(data_dir) / profile_id / "profile.md"
        try:
            markdown = path.read_text(encoding="utf-8")
        except OSError:
            continue
        path.write_text(
            update_config_keys(
                markdown,
                {
                    "lifecycle": "pool",
                    "min_active": DEFAULT_MIN_ACTIVE,
                    "max_active": max_active,
                },
            ),
            encoding="utf-8",
        )
        created.append(profile_id)

    if not created:
        return PoolDefaults(
            max_active=max_active,
            problem=(
                "no signed-in agent CLI has a standard-high worker to pool; sign in to one "
                "(or add one with `aq install --with provider.<name>`), then rerun `aq install`"
            ),
        )
    return PoolDefaults(created=tuple(created), max_active=max_active)


__all__ = [
    "DEFAULT_MIN_ACTIVE",
    "DEFAULT_POOL_CLASS",
    "FALLBACK_MAX_ACTIVE",
    "PoolDefaults",
    "configured_pools",
    "ensure_default_pools",
]
