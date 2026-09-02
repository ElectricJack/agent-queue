"""Playbook V2 semantic-graph commands — graph, health, diff, activation,
pending events and run overlays.

Package 5 of the Playbook V2 roadmap
(``docs/superpowers/plans/2026-09-01-playbook-v2-graph-api-ui.md``) ships the
operator surface for the typed V2 artifact: an event-grouped rule graph with
exact executable edges, a semantic artifact diff, explicit activation by hash,
pending-event operator actions, and run overlays pinned to the artifact the run
actually executed.

**Surface, not semantics.** Every command here is a projection or one of two
narrow operator writes.  None of them compiles, validates, executes or repairs
anything — §3.3 of the child plan.  The child plan's §2 reconciliation records
why these are ``CommandHandler`` commands rather than hand-written FastAPI
routes: ``src/api/codegen.py`` turns every categorised command into
``POST /api/playbook/<stripped-name>``, an ``aq playbook <verb>`` CLI verb and
an MCP tool at once, and that is the only path the committed ``openapi.json``
and both generated clients cover.

**Current state.** The artifact model (Package 2), artifact store / activation
records / receipts / pending events (Package 3) and the engine (Package 4) have
not landed on ``main`` — only their child plans have.  Every command below is
therefore fully wired end to end (registration, HTTP route, CLI verb, generated
clients, feature flags, argument validation, scope) and returns
``V2_STORAGE_UNAVAILABLE_ERROR`` at the single seam where it would read that
state.  ``_v2_storage_unavailable`` is that seam: the package that lands the
artifact store replaces its body and fills in the projections, without touching
the wire contract in ``src/api/models/playbook_v2.py``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


#: Exact error strings.  Operators, docs and the dashboard match on these, so
#: do not reword them without updating the child plan §5.4 and §8.
V2_API_DISABLED_ERROR = "playbook v2 api is disabled (playbooks.v2_api=false)"
V2_WRITES_DISABLED_ERROR = (
    "playbook v2 activation writes are disabled (playbooks.v2_activation_writes=false)"
)
V2_STORAGE_UNAVAILABLE_ERROR = (
    "playbook v2 artifact storage is unavailable: the typed artifact model, "
    "artifact store and run receipts (playbook V2 roadmap packages 2-4) are not "
    "present in this build"
)

#: The seven command names this mixin owns, in child-plan §4.8 order.  Imported
#: by ``src/commands/handler.py`` (feature pause) and by the tests that pin the
#: registration surface.
PLAYBOOK_V2_COMMANDS: frozenset[str] = frozenset(
    {
        "playbook_v2_graph",
        "playbook_activation_health",
        "playbook_activate",
        "playbook_artifact_diff",
        "playbook_pending_events",
        "playbook_pending_event_action",
        "playbook_run_overlay",
    }
)

#: Activation scopes, matching ``ActivationStateDTO.scope``.
_VALID_SCOPES: frozenset[str] = frozenset({"system", "project", "agent_type"})

#: ``ActivationHealthValue`` — child plan §4.4.
_VALID_HEALTH: frozenset[str] = frozenset(
    {"ready", "question_required", "invalid", "disabled", "stale_contract", "unavailable"}
)

#: ``PendingReason`` — child plan §4.6.
_VALID_PENDING_REASONS: frozenset[str] = frozenset(
    {"stale_contract", "invalid_artifact", "disabled", "unavailable", "question_required"}
)

#: ``PendingAction`` — child plan §4.6.
_VALID_PENDING_ACTIONS: frozenset[str] = frozenset({"dispatch", "discard"})

#: Full ``sha256:<64 hex>`` form.  Hashes are never truncated on the wire
#: (child plan §4 conventions), so a truncated hash is a client bug worth an
#: explicit error rather than a silent miss.
_SHA_PREFIX = "sha256:"
_SHA_HEX_LEN = 64


def _clean_str(args: dict, key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


def _validate_sha(value: str, field: str) -> str | None:
    """Return an error message when *value* is not a full artifact hash."""
    if not value.startswith(_SHA_PREFIX):
        return f"{field} must be a full 'sha256:<64 hex>' digest"
    hexpart = value[len(_SHA_PREFIX) :]
    if len(hexpart) != _SHA_HEX_LEN or any(c not in "0123456789abcdef" for c in hexpart):
        return f"{field} must be a full 'sha256:<64 hex>' digest"
    return None


class PlaybookV2CommandsMixin:
    """Playbook V2 semantic-graph command methods mixed into CommandHandler."""

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def _v2_api_enabled(self) -> bool:
        playbooks = getattr(self.config, "playbooks", None)
        return bool(getattr(playbooks, "v2_api", False))

    def _v2_activation_writes_enabled(self) -> bool:
        playbooks = getattr(self.config, "playbooks", None)
        return bool(getattr(playbooks, "v2_activation_writes", False))

    def _v2_storage_unavailable(self) -> dict:
        """The single seam onto Package 2-4 state.

        Every command reaches this only after its arguments have validated and
        both feature flags have passed — the point where it would load the
        pinned artifact, its activation record and its receipts.  Those live in
        ``src/playbooks/definition.py``, ``artifact_store.py``, ``activation.py``
        and ``src/database/queries/playbook_{artifact,run}_queries.py``, none of
        which exist on ``main`` yet.  The package that lands them replaces this
        method with the real lookup and fills in the projections
        (``graph_projection.project_graph``, ``artifact_diff.diff_artifacts``,
        ``run_overlay.project_overlay``) behind the wire contract already frozen
        in ``src/api/models/playbook_v2.py``.
        """
        return {"error": V2_STORAGE_UNAVAILABLE_ERROR}

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def _cmd_playbook_v2_graph(self, args: dict) -> dict:
        """Return the event-grouped semantic graph of one playbook artifact.

        Projects the immutable V2 artifact into rule clusters, contract-derived
        node explanations and one edge per declared transition, together with
        the artifact reference and the activation state it belongs to.  Defaults
        to the playbook's currently **active** artifact; ``artifact_sha256``
        selects a specific one, which is how a run overlay pins its graph.

        Args:
            playbook_id: Required — the playbook identifier to project.
            artifact_sha256: Optional — project this exact artifact instead of
                the active one. Full ``sha256:<64 hex>`` form.
            event_type: Optional — narrow ``rules``/``nodes``/``edges`` to the
                rules triggered by this event. ``event_groups`` still lists
                every event, and no reachable branch is dropped.
            direction: Layout direction — ``"TD"`` (top-down) or ``"LR"``
                (left-right). Default: ``"TD"``.
            include_advanced: Include the canonical typed step body in
                ``advanced.typed_step``. Default: ``True``; ``False`` leaves the
                field present but empty so the response type never changes.
        """
        if not self._v2_api_enabled():
            return {"error": V2_API_DISABLED_ERROR}

        playbook_id = _clean_str(args, "playbook_id")
        if not playbook_id:
            return {"error": "playbook_id is required"}

        artifact_sha256 = _clean_str(args, "artifact_sha256")
        if artifact_sha256:
            invalid = _validate_sha(artifact_sha256, "artifact_sha256")
            if invalid:
                return {"error": invalid}

        direction = (_clean_str(args, "direction") or "TD").upper()
        if direction not in ("TD", "LR"):
            return {"error": f"Invalid direction '{direction}'. Valid: TD, LR"}

        return self._v2_storage_unavailable()

    async def _cmd_playbook_activation_health(self, args: dict) -> dict:
        """List playbook activations with their computed health.

        ``enabled`` and ``health`` are independent: a disabled activation still
        reports the health it would have.  ``health="disabled"`` means there is
        no active artifact at all.

        Args:
            playbook_id: Optional — one playbook. All activations when absent.
            scope: Optional — ``system``, ``project`` or ``agent_type``.
            health: Optional — filter to one health value (``ready``,
                ``question_required``, ``invalid``, ``disabled``,
                ``stale_contract``, ``unavailable``).
        """
        if not self._v2_api_enabled():
            return {"error": V2_API_DISABLED_ERROR}

        scope = _clean_str(args, "scope")
        if scope and scope not in _VALID_SCOPES:
            return {"error": f"Invalid scope '{scope}'. Valid: {', '.join(sorted(_VALID_SCOPES))}"}

        health = _clean_str(args, "health")
        if health and health not in _VALID_HEALTH:
            return {
                "error": f"Invalid health '{health}'. Valid: {', '.join(sorted(_VALID_HEALTH))}"
            }

        return self._v2_storage_unavailable()

    async def _cmd_playbook_artifact_diff(self, args: dict) -> dict:
        """Diff two playbook artifacts semantically, before activation.

        Read-only: the diff never activates anything.  Rules match by
        ``rule_id``, steps by ``(rule_id, step_id)`` and edges by edge id, so
        reordering an unordered map is ``unchanged``.  Presentation-only changes
        (titles, labels, help text) report ``executable=False`` and do not block
        activation.

        Args:
            playbook_id: Required — the playbook that owns both artifacts.
            target_sha256: Required — the artifact under review.
            base_sha256: Optional — defaults to the currently active artifact;
                absent entirely for a playbook's first artifact.
        """
        if not self._v2_api_enabled():
            return {"error": V2_API_DISABLED_ERROR}

        playbook_id = _clean_str(args, "playbook_id")
        if not playbook_id:
            return {"error": "playbook_id is required"}

        target_sha256 = _clean_str(args, "target_sha256")
        if not target_sha256:
            return {"error": "target_sha256 is required"}
        invalid = _validate_sha(target_sha256, "target_sha256")
        if invalid:
            return {"error": invalid}

        base_sha256 = _clean_str(args, "base_sha256")
        if base_sha256:
            invalid = _validate_sha(base_sha256, "base_sha256")
            if invalid:
                return {"error": invalid}

        return self._v2_storage_unavailable()

    async def _cmd_playbook_pending_events(self, args: dict) -> dict:
        """List events held because no artifact could run them.

        Pending events are retained, visible and operable — they are never
        silently dropped (roadmap §2).  Read-only.

        Args:
            playbook_id: Optional — one playbook. All playbooks when absent.
            reason: Optional — ``stale_contract``, ``invalid_artifact``,
                ``disabled``, ``unavailable`` or ``question_required``.
            limit: Max events to return. Default: 100.
        """
        if not self._v2_api_enabled():
            return {"error": V2_API_DISABLED_ERROR}

        reason = _clean_str(args, "reason")
        if reason and reason not in _VALID_PENDING_REASONS:
            return {
                "error": (
                    f"Invalid reason '{reason}'. "
                    f"Valid: {', '.join(sorted(_VALID_PENDING_REASONS))}"
                )
            }

        raw_limit = args.get("limit", 100)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return {"error": "limit must be an integer"}
        if limit < 1:
            return {"error": "limit must be >= 1"}

        return self._v2_storage_unavailable()

    async def _cmd_playbook_run_overlay(self, args: dict) -> dict:
        """Return one run's execution overlay, pinned to the artifact it ran.

        The definition is loaded by the run's own ``artifact_sha256``, never by
        the playbook's current activation, so an overlay is never projected onto
        a newer artifact.  ``artifact_is_active=False`` is how the dashboard
        knows to warn.  Loop iterations are listed individually rather than
        collapsed into one misleading status.

        Args:
            run_id: Required — the V2 run to overlay.
            receipt_limit: Max receipts returned, newest first. Default: 500;
                ``truncated`` reports when more exist.
        """
        if not self._v2_api_enabled():
            return {"error": V2_API_DISABLED_ERROR}

        run_id = _clean_str(args, "run_id")
        if not run_id:
            return {"error": "run_id is required"}

        raw_limit = args.get("receipt_limit", 500)
        try:
            receipt_limit = int(raw_limit)
        except (TypeError, ValueError):
            return {"error": "receipt_limit must be an integer"}
        if receipt_limit < 1:
            return {"error": "receipt_limit must be >= 1"}

        return self._v2_storage_unavailable()

    # ------------------------------------------------------------------
    # Operator writes — separately feature-gated (child plan §7.3, §8)
    # ------------------------------------------------------------------

    async def _cmd_playbook_activate(self, args: dict) -> dict:
        """Activate one reviewed artifact hash for a playbook.

        Activation is an explicit database operation against a reviewed hash;
        compilation never activates.  The command refuses unless
        ``playbooks.v2_activation_writes`` is on, the target artifact's health
        is not ``invalid``, and either the diff against the currently active
        artifact carries no executable change or the caller passed
        ``acknowledge_diff`` equal to ``artifact_sha256`` — the literal hash, so
        an acknowledgement cannot be replayed against a different artifact.

        Args:
            playbook_id: Required — the playbook to activate against.
            artifact_sha256: Required — the reviewed artifact hash.
            enabled: Whether the activation is enabled. Default: ``True``.
            acknowledge_diff: Required when the diff against the active
                artifact is executable; must equal ``artifact_sha256``.
        """
        if not self._v2_api_enabled():
            return {"error": V2_API_DISABLED_ERROR}
        if not self._v2_activation_writes_enabled():
            return {"error": V2_WRITES_DISABLED_ERROR}

        playbook_id = _clean_str(args, "playbook_id")
        if not playbook_id:
            return {"error": "playbook_id is required"}

        artifact_sha256 = _clean_str(args, "artifact_sha256")
        if not artifact_sha256:
            return {"error": "artifact_sha256 is required"}
        invalid = _validate_sha(artifact_sha256, "artifact_sha256")
        if invalid:
            return {"error": invalid}

        acknowledge_diff = _clean_str(args, "acknowledge_diff")
        if acknowledge_diff and acknowledge_diff != artifact_sha256:
            return {
                "error": (
                    "acknowledge_diff must equal artifact_sha256; an "
                    "acknowledgement cannot be replayed against another artifact"
                )
            }

        return self._v2_storage_unavailable()

    async def _cmd_playbook_pending_event_action(self, args: dict) -> dict:
        """Dispatch or discard held pending events.

        ``dispatch`` re-enters the engine's own event dispatch with the
        server-derived principal of this request — it never re-implements
        matching and never adopts a principal from the stored event.
        ``discard`` records the resolution without dispatching.

        Args:
            action: Required — ``dispatch`` or ``discard``.
            pending_event_ids: Required — non-empty list of pending event ids.
        """
        if not self._v2_api_enabled():
            return {"error": V2_API_DISABLED_ERROR}
        if not self._v2_activation_writes_enabled():
            return {"error": V2_WRITES_DISABLED_ERROR}

        action = _clean_str(args, "action")
        if not action:
            return {"error": "action is required"}
        if action not in _VALID_PENDING_ACTIONS:
            return {
                "error": (
                    f"Invalid action '{action}'. "
                    f"Valid: {', '.join(sorted(_VALID_PENDING_ACTIONS))}"
                )
            }

        raw_ids = args.get("pending_event_ids")
        if isinstance(raw_ids, str):
            raw_ids = [raw_ids]
        if not isinstance(raw_ids, list):
            return {"error": "pending_event_ids is required and must be a list"}
        pending_event_ids = [i.strip() for i in raw_ids if isinstance(i, str) and i.strip()]
        if not pending_event_ids:
            return {"error": "pending_event_ids must be a non-empty list of event ids"}

        return self._v2_storage_unavailable()
