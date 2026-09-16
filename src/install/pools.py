"""Default worker pools for the agent CLIs this machine has signed in.

Work reaches workers through pools: a pool worker claims the next ready task
itself (``aq task claim``).  Every derived worker profile ("rung") is seeded as
``lifecycle: task`` and becomes a pool only when someone sizes it, so a fresh
install had active worker profiles and no pool for a single task to run on.

:func:`ensure_default_pools` runs at the end of `aq install`: **every active
rung** -- every intelligence class of every installed and signed-in agent CLI
-- becomes a pool that scales from zero to the machine's tuned concurrent-agent
limit.  All of them, not one per CLI, because the routing playbook may send a
task to any active rung (a hard task to ``deep-high-claude``), and a rung with
no pool is a task that never runs; ``min_active: 0`` keeps that free while
idle, and ``swarm.global_max_active`` still caps the fleet.  It also means the
dashboard's pool list -- which offers only pool profiles -- shows every worker
the machine can run.

It writes the rung's ``## Config`` in the vault -- the source of truth the
daemon's profile watcher syncs from -- so it applies with or without a running
daemon.  It remembers which rungs it has made pools (``pool-defaults.json`` in
AQ's data directory), so a rung an operator later turns back to
``lifecycle: task``, or sizes differently, keeps that choice on every rerun;
a rung that becomes active later (a newly signed-in CLI, a new class) is
pooled when it first appears.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MIN_ACTIVE = 0
#: Records the rungs this module has made pools, beside config.yaml.
OFFERED_FILENAME = "pool-defaults.json"
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


def _offered(data_dir: Path) -> set[str]:
    try:
        payload = json.loads((data_dir / OFFERED_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    offered = payload.get("offered") if isinstance(payload, dict) else None
    return {str(item) for item in offered} if isinstance(offered, list) else set()


def _record_offered(data_dir: Path, offered: set[str]) -> None:
    path = data_dir / OFFERED_FILENAME
    path.write_text(
        json.dumps({"schema_version": 1, "offered": sorted(offered)}, indent=2) + "\n",
        encoding="utf-8",
    )


def ensure_default_pools(
    data_dir: str | Path,
    activations: Iterable[Any],
    config: Mapping[str, Any],
) -> PoolDefaults:
    """Make every active rung a pool, once per rung."""
    from src.profiles.parser import update_config_keys

    root = Path(data_dir)
    swarm = config.get("swarm")
    if not (isinstance(swarm, Mapping) and swarm.get("enabled") is True):
        return PoolDefaults(
            existing=configured_pools(root),
            problem=(
                "swarm.enabled is not true in config.yaml, so a pool would never be started; "
                "set it with `aq system config edit`, then rerun `aq install`"
            ),
        )

    active = sorted(
        {
            activation.profile.id
            for activation in activations
            if getattr(activation, "active", False)
        }
    )
    max_active = _max_active(config)
    already = set(configured_pools(root))
    offered = _offered(root)
    created = []
    for profile_id in active:
        if profile_id in already or profile_id in offered:
            # A pool already, or one this module made before and the operator
            # has since changed: either way, not ours to touch again.
            continue
        path = _profiles_root(root) / profile_id / "profile.md"
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
    # Pools that predate the record count as offered too: otherwise an
    # operator who later turned one back to `task` would see it re-pooled.
    remembered = offered | set(created) | (already & set(active))
    if remembered != offered:
        _record_offered(root, remembered)

    existing = tuple(sorted(set(configured_pools(root)) - set(created)))
    if not created and not existing:
        return PoolDefaults(
            max_active=max_active,
            problem=(
                "no signed-in agent CLI has a worker profile to pool; sign in to one "
                "(or add one with `aq install --with provider.<name>`), then rerun `aq install`"
            ),
        )
    return PoolDefaults(created=tuple(created), existing=existing, max_active=max_active)


__all__ = [
    "DEFAULT_MIN_ACTIVE",
    "OFFERED_FILENAME",
    "FALLBACK_MAX_ACTIVE",
    "PoolDefaults",
    "configured_pools",
    "ensure_default_pools",
]
