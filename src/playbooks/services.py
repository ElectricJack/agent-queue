"""What a playbook run needs from the daemon, bundled (llm-direct-path §5)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

from src.integration.models import HierarchicalIntegrationPolicy

if TYPE_CHECKING:
    from src.commands.handler import CommandHandler
    from src.llm import LLMClient
    from src.llm_logger import LLMLogger
    from src.tools.registry import ToolRegistry

#: Navigation tools the old chat loop special-cased; a playbook node never gets them.
_EXCLUDED_TOOLS = frozenset({"load_tools", "reply_to_user"})


#: Event families the timer service emits with ``project_id: null``; they are
#: system-scoped by nature and a project-scoped playbook may trigger on them.
GLOBAL_EVENT_PREFIXES = ("timer.", "cron.")

# These two definitions are the command-bearing integration lifecycle.  They
# are shared at system scope, but their authority always comes from a project's
# frozen policy route.  Other system playbooks (default review, notifications,
# observers) retain normal event-to-scope fanout.
INTEGRATION_LIFECYCLE_PLAYBOOK_IDS = frozenset(
    {"hierarchical-delivery", "root-integration-train"}
)
_PARENT_INTEGRATION_EVENTS = frozenset(
    {
        "task.completed",
        "task.failed",
        "task.child_added",
        "task.parent_checkpointed",
        "delivery.ready",
        "delivery.applied",
        "task.integration_ready",
        "task.integration_verified",
        "integration.ci_completed",
        "integration.repair_exhausted",
        "integration.repair_deadline_due",
        "integration.resolution_push_observed",
        "integration.repair_delegate_closed",
    }
)
_ROOT_INTEGRATION_EVENTS = frozenset(
    {
        "integration.sweep_due",
        "integration.sealed",
        "integration.candidate_green",
        "integration.candidate_red",
        "integration.repair_exhausted",
        "integration.batch_promoted",
        "integration.cleanup_requested",
    }
)
_POLICY_ROUTE_FALLBACK_EVENTS = frozenset(
    {
        "task.completed",
        "task.failed",
        "task.child_added",
        "task.parent_checkpointed",
        "integration.sweep_due",
    }
)


@dataclass(frozen=True, slots=True)
class IntegrationRouteTarget:
    """One project-authorized activation address and immutable artifact."""

    activation_id: str | None
    playbook_id: str
    scope: str
    scope_identifier: str
    artifact_sha256: str

    def matches_activation(self, row: Any) -> bool:
        return (
            row.get("playbook_id") == self.playbook_id
            and row.get("scope") == self.scope
            and (row.get("scope_identifier") or "") == self.scope_identifier
        )


def is_integration_route_event(event_type: str) -> bool:
    return event_type in _PARENT_INTEGRATION_EVENTS | _ROOT_INTEGRATION_EVENTS


async def resolve_integration_route(
    db: Any, event_type: str, event: Any
) -> IntegrationRouteTarget | None:
    """Resolve project authority for one integration-lifecycle dispatch.

    Operation-bound events use the operation's immutable route.  Only the
    initial root sweep and ordinary child terminal events may fall back to the
    project's current policy because no integration operation exists yet.
    Missing/invalid project identity, disabled integration, and invalid policy
    all fail closed.
    """

    if not is_integration_route_event(event_type):
        return None
    project_id = event.get("project_id") if isinstance(event, dict) else None
    if not isinstance(project_id, str) or not project_id.strip():
        return None
    project = await db.get_project(project_id)
    if (
        project is None
        or project.hierarchical_integration_mode not in {"hierarchy", "train"}
    ):
        return None
    try:
        policy = HierarchicalIntegrationPolicy.model_validate(
            project.hierarchical_integration_policy
        )
    except (TypeError, ValueError):
        return None

    operation_id = event.get("operation_id")
    if isinstance(operation_id, str) and operation_id.strip():
        row = await db.get_integration_operation_artifact_route(operation_id)
        if row is not None:
            scope = row.get("scope")
            identifier = row.get("scope_identifier")
            sha = row.get("artifact_sha256")
            activation_id = row.get("activation_id")
            playbook_id = row.get("playbook_id")
            if (
                isinstance(playbook_id, str)
                and playbook_id
                # The historical activation is optional audit metadata. The
                # runtime admits the pinned artifact through the currently
                # enabled activation at this stable playbook/scope address.
                and (activation_id is None or isinstance(activation_id, str))
                and isinstance(sha, str)
                and sha
                and (
                    (scope == "system" and identifier == "")
                    or (scope == "project" and identifier == project_id)
                )
            ):
                if scope == "system" and row.get("project_id") != project_id:
                    return None
                return IntegrationRouteTarget(
                    activation_id=activation_id,
                    playbook_id=playbook_id,
                    scope=scope,
                    scope_identifier=identifier,
                    artifact_sha256=sha,
                )
            return None
    if event_type not in _POLICY_ROUTE_FALLBACK_EVENTS:
        return None
    if event_type == "integration.sweep_due" and (
        not isinstance(operation_id, str) or not operation_id.strip()
    ):
        return None

    boundary = policy.root if event_type == "integration.sweep_due" else policy.parent
    route = boundary.route
    if not route.is_available_to_project(project_id):
        return None
    return IntegrationRouteTarget(
        activation_id=route.activation_id,
        playbook_id=route.playbook_id,
        scope=route.scope,
        scope_identifier=route.scope_identifier,
        artifact_sha256=route.artifact.artifact_sha256,
    )


def is_global_event(event_type: str) -> bool:
    return str(event_type or "").startswith(GLOBAL_EVENT_PREFIXES)


class DatabaseActivationSource:
    """Project Package 3 activation rows into the engine's tiny read contract."""

    def __init__(self, db: Any) -> None:
        self._db = db

    async def ready_activations(
        self, event_type: str, event: Any | None = None
    ) -> list[Any]:
        rows = await self._db.list_playbook_activations(enabled_only=True)
        refs: list[Any] = []
        event = event or {}
        project_id = event.get("project_id")
        agent_type = event.get("agent_type")
        # Timer and cron events carry ``project_id: null`` because they are
        # inherently system-scoped (``src/timer_service.py``): a project-scoped
        # playbook may trigger on them, firing once globally.  Requiring the
        # project id to match here meant no project playbook ever received a
        # timer tick, which silently parked ``pr-merge-sweep`` and
        # ``ci-main-sentinel`` forever.  Every other event without a project id
        # still reaches system playbooks only.
        global_event = project_id is None and is_global_event(event_type)
        integration_event = is_integration_route_event(event_type)
        integration_route = (
            await resolve_integration_route(self._db, event_type, event)
            if integration_event
            else None
        )
        lifecycle_ids = INTEGRATION_LIFECYCLE_PLAYBOOK_IDS | (
            {integration_route.playbook_id} if integration_route is not None else set()
        )
        for row in rows:
            health = getattr(row.get("health"), "value", row.get("health"))
            artifact_sha256 = row.get("active_artifact_sha256")
            if health != "ready" or not artifact_sha256:
                continue
            scope = row.get("scope")
            identifier = row.get("scope_identifier") or ""
            if integration_event and row.get("playbook_id") in lifecycle_ids:
                if integration_route is None or not integration_route.matches_activation(row):
                    continue
                artifact_sha256 = integration_route.artifact_sha256
            # A global timer has no event project.  The activation address is
            # therefore the only authoritative project identity for the
            # per-project legacy merge sweep.
            if scope == "project" and row.get("playbook_id") == "pr-merge-sweep":
                suppression = await self._db.get_integration_legacy_suppression(identifier)
                if (suppression and suppression.get("merge_sweep_suppressed")) or await self.development_reviews_suppressed(identifier):
                    continue
            if scope == "project" and identifier != project_id and not global_event:
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

    async def development_reviews_suppressed(self, project_id: str) -> bool:
        project = await self._db.get_project(project_id)
        return getattr(project, "hierarchical_integration_mode", "disabled") == "development"

    async def legacy_final_review_suppressed(self, project_id: str) -> bool:
        if await self.development_reviews_suppressed(project_id):
            return True
        suppression = await self._db.get_integration_legacy_suppression(project_id)
        return bool(suppression and suppression.get("final_review_route_suppressed"))

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
