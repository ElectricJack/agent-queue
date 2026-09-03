"""What a playbook run needs from the daemon, bundled (llm-direct-path §5)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

if TYPE_CHECKING:
    from src.commands.handler import CommandHandler
    from src.llm import LLMClient
    from src.llm_logger import LLMLogger
    from src.tools.registry import ToolRegistry

#: Navigation tools the old chat loop special-cased; a playbook node never gets them.
_EXCLUDED_TOOLS = frozenset({"load_tools", "reply_to_user"})


def v2_engine_enabled(config: Any) -> bool:
    """Whether production callers may enter the V2 engine.

    The subsystem switch remains authoritative: accepting ``v2_engine`` while
    playbooks as a whole are paused would make a no-op look like a successful
    cutover.
    """
    playbooks = getattr(config, "playbooks", None)
    return (
        getattr(playbooks, "enabled", False) is True
        and getattr(playbooks, "v2_engine", False) is True
    )


class DatabaseActivationSource:
    """Project Package 3 activation rows into the engine's tiny read contract."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def ready_activations(
        self, _event_type: str, event: Any | None = None
    ) -> list[Any]:
        rows = await self._db.list_playbook_activations(enabled_only=True)
        refs: list[Any] = []
        event = event or {}
        project_id = event.get("project_id")
        agent_type = event.get("agent_type")
        for row in rows:
            health = getattr(row.get("health"), "value", row.get("health"))
            artifact_sha256 = row.get("active_artifact_sha256")
            if health != "ready" or not artifact_sha256:
                continue
            scope = row.get("scope")
            identifier = row.get("scope_identifier") or ""
            if scope == "project" and identifier != project_id:
                continue
            if scope == "agent_type" and (
                project_id is None or identifier != agent_type
            ):
                continue
            if scope not in {"system", "project", "agent_type"}:
                continue
            ref = await self._db.get_playbook_artifact(artifact_sha256)
            if ref is not None:
                refs.append(ref)
        return refs

    async def artifact_by_sha(self, artifact_sha256: str) -> Any | None:
        """Resolve an immutable artifact independently of current activation."""
        return await self._db.get_playbook_artifact(artifact_sha256)

    async def artifact_for(
        self, playbook_id: str, *, scope_identifier: str | None = None
    ) -> Any | None:
        """Return the best ready artifact for one synchronous caller."""
        rows = await self._db.list_playbook_activations(enabled_only=True)
        candidates = []
        for row in rows:
            health = getattr(row.get("health"), "value", row.get("health"))
            if row.get("playbook_id") != playbook_id or health != "ready":
                continue
            scope = row.get("scope")
            identifier = row.get("scope_identifier") or ""
            if scope == "project" and scope_identifier != identifier:
                continue
            if scope not in {"project", "system"}:
                continue
            candidates.append((0 if scope == "project" else 1, row))
        for _priority, row in sorted(candidates, key=lambda item: item[0]):
            artifact_sha256 = row.get("active_artifact_sha256")
            if artifact_sha256:
                ref = await self._db.get_playbook_artifact(artifact_sha256)
                if ref is not None:
                    return ref
        return None


def bind_pending_event_policy(db: Any, playbooks: Any) -> None:
    """Push the configured pending-event quota and overflow policy onto *db*.

    The repository carries its own defaults so a test adapter needs no
    config, which also means an unbound adapter silently ignores the
    operator's ``playbooks:`` section.  This is the one place the daemon
    builds V2 storage, so it is where the two are joined.  Adapters that
    predate the setters are left alone rather than failing the daemon.
    """
    quota = getattr(db, "set_playbook_pending_event_quota", None)
    if callable(quota):
        quota(playbooks.v2_max_pending_events_per_playbook)
    overflow = getattr(db, "set_playbook_pending_event_overflow", None)
    if callable(overflow):
        try:
            overflow(playbooks.v2_pending_event_on_overflow)
        except ValueError:
            # ``AppConfig.validate()`` already reports an unknown policy as a
            # config error.  Refusing to build the engine over it would turn
            # one bad string into a dead daemon, so keep the repository's
            # default and let the validation report stand.
            logging.getLogger(__name__).warning(
                "ignoring unknown playbooks.v2_pending_event_on_overflow %r; "
                "keeping the repository default",
                playbooks.v2_pending_event_on_overflow,
            )


def build_v2_engine(
    *, config: Any, db: Any, handler: Any, llm: Any = None, bus: Any = None
) -> Any:
    """Build the one production V2 engine from daemon-owned dependencies."""
    from src.commands.authorization import CommandHandlerResolver
    from src.commands.contracts import CONTRACTS
    from src.playbooks.artifact_store import ArtifactStore
    from src.playbooks.engine import PlaybookEngine
    from src.playbooks.executors.base import EngineServices

    cached = getattr(handler, "__dict__", {}).get("_v2_playbook_engine")
    if cached is not None:
        return cached

    playbooks = config.playbooks
    bind_pending_event_policy(db, playbooks)
    services = EngineServices(
        contracts=CONTRACTS,
        clock=time.time,
        artifact_store=ArtifactStore(
            config.compiled_root,
            max_artifact_bytes=playbooks.v2_max_artifact_bytes,
        ),
        llm=llm,
        handler=handler,
        db=db,
        bus=bus,
        resolver=CommandHandlerResolver(handler),
        authorization_mode=getattr(
            getattr(config, "security", None), "capability_enforcement", "audit"
        ),
    )
    engine = PlaybookEngine(
        services=services,
        runs=db,
        waits=db,
        activations=DatabaseActivationSource(db),
        cancellation_grace_seconds=playbooks.cancellation_grace_seconds,
    )
    try:
        setattr(handler, "_v2_playbook_engine", engine)
    except (AttributeError, TypeError):
        pass
    return engine


async def load_v2_snapshot(db: Any, run_id: str) -> Any | None:
    """Load a V2 run without mistaking permissive test doubles for one."""
    from src.playbooks.run_state import RunSnapshot

    load_run = getattr(db, "load_run", None)
    if not callable(load_run):
        return None
    try:
        snapshot = await load_run(run_id)
    except (AttributeError, TypeError):
        return None
    return snapshot if isinstance(snapshot, RunSnapshot) else None


@dataclass
class PlaybookServices:
    llm: "LLMClient"
    handler: "CommandHandler"
    tool_registry: "ToolRegistry"
    llm_logger: "LLMLogger | None" = None
    runtimes: Any = None  # RuntimeRegistry for harness-less one-shot node sessions

    def node_tools(self, allowed: list[str] | None) -> list[dict]:
        """Tool definitions for one node: exactly the names in ``allowed``.

        Two behaviours changed in Playbook V2 Package 0 §3.1/§5.4:

        - ``allowed is None`` (no policy declared) now means **no tools**, not
          the registry's full catalogue.  "Missing means everything" is the
          same default-open shape as an empty capability set meaning "all",
          and the spec forbids it.  A playbook that needs tools names them.
        - An unknown name is **filtered** rather than raised on.  A policy is
          an allowlist, and a name the registry does not (yet) know is simply
          not granted; raising turned a narrowing intent into a hard failure
          at run time.
        """
        known = {t["name"]: t for t in self.tool_registry.get_all_tools()}
        if allowed is None:
            return []
        tools = [known[n] for n in allowed if n in known]
        return [t for t in tools if t["name"] not in _EXCLUDED_TOOLS]

    @classmethod
    def for_tests(cls, llm: "LLMClient") -> "PlaybookServices":
        registry = MagicMock()
        registry.get_core_tools.return_value = []
        registry.get_all_tools.return_value = []
        handler = MagicMock()
        handler.execute = AsyncMock(return_value={"success": True})
        return cls(llm=llm, handler=handler, tool_registry=registry)
