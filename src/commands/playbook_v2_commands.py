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

**Current state.** Package 3 has landed on ``main``: the artifact store
(``src/playbooks/artifact_store.py``), activation records
(``src/playbooks/activation.py``, ``playbook_activations``) and
``src/database/queries/playbook_artifact_queries.py``.  The typed artifact model
(Package 2, ``src/playbooks/definition.py``), the explanation and contract
registry (Package 1), and the engine, receipts, V2 runs and pending events
(Package 4) have not — only their child plans have.  Every command below is
therefore fully wired end to end (registration, HTTP route, CLI verb, generated
clients, feature flags, argument validation, scope) and returns
``V2_STORAGE_UNAVAILABLE_ERROR`` at the single seam where it would read that
state.  The one exception is ``playbook_activation_health``: activation health
needs nothing but Package 3's own rows and files, so with
``playbooks.v2_storage_enabled`` on it reads real activations and computes
health against the live contract and capability-profile registries.  ``_v2_storage_unavailable`` is that seam: the task that lands the
projections (child plan §16.2, §16.3) replaces its body and fills in
``graph_projection``, ``artifact_diff`` and ``run_overlay``, without touching
the wire contract in ``src/api/models/playbook_v2.py``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.playbooks.authoring import PlaybookSource, SourceError
from src.playbooks.definition import (
    DuplicateJsonKey,
    PlaybookDefinition,
    artifact_sha256,
    load_definition_json,
)
from src.playbooks.pipeline_lowering import shadow_compile
from src.playbooks.proposal import DuplicateSemanticKey, load_semantic_body_json, propose
from src.playbooks.validation import (
    Diagnostic,
    RegisteredEventLookup,
    RegistryContractLookup,
    VaultProfileLookup,
    validate_definition,
)

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

#: Package 2's review-only compiler surface.  Kept separate from the Package 5
#: projection constant because several API-contract tests intentionally pin
#: that seven-command set.
PLAYBOOK_V2_COMPILER_COMMANDS: frozenset[str] = frozenset(
    {
        "playbook_v2_validate",
        "playbook_v2_propose",
        "playbook_v2_shadow_compile",
    }
)

V2_COMPILER_DISABLED_ERROR = "playbook v2 compiler is disabled"

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


def _diagnostic_dict(diagnostic: Diagnostic) -> dict[str, Any]:
    return {
        "severity": diagnostic.severity,
        "code": diagnostic.code,
        "message": diagnostic.message,
        "rule_id": diagnostic.rule_id,
        "step_id": diagnostic.step_id,
        "field": diagnostic.field,
        "source": diagnostic.source.model_dump(mode="json") if diagnostic.source else None,
    }


def _diagnostic_counts(diagnostics: list[Diagnostic]) -> dict[str, int]:
    return {
        severity: sum(d.severity == severity for d in diagnostics)
        for severity in ("error", "warning", "question", "info")
    }


def _command_error(message: str, *, field: str | None = None) -> dict[str, Any]:
    diagnostic = Diagnostic(
        "error", "ambiguous_prose", message, field=f"/{field}" if field else None
    )
    return {
        "success": False,
        "artifact_sha256": None,
        "counts": _diagnostic_counts([diagnostic]),
        "diagnostics": [_diagnostic_dict(diagnostic)],
    }


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

    def _v2_activation_storage_ready(self) -> bool:
        """Whether activation health can actually be read from this build.

        Health needs only Package 3 state — ``playbook_activations`` plus the
        artifact rows and files — so it is the one projection that does not
        wait for the rest of ``_v2_storage_unavailable``\'s seam.  The gate is
        the storage flag itself plus the query the read path calls, so a
        database adapter without the V2 tables still reports the seam error
        rather than raising.
        """
        playbooks = getattr(self.config, "playbooks", None)
        if not bool(getattr(playbooks, "v2_storage_enabled", False)):
            return False
        return hasattr(self.db, "list_playbook_activations") and hasattr(
            self.db, "get_playbook_artifact_row"
        )

    def _v2_compiler_enabled(self) -> bool:
        playbooks = getattr(self.config, "playbooks", None)
        return bool(getattr(playbooks, "v2_compiler_enabled", False))

    def _v2_vault_root(self) -> Path:
        configured = getattr(self.config, "vault_root", None)
        if configured:
            return Path(configured).resolve()
        return (Path(self.config.data_dir) / "vault").resolve()

    def _v2_resolve_vault_path(self, raw: Any, field: str) -> tuple[Path | None, str | None]:
        if not isinstance(raw, str) or not raw.strip():
            return None, f"{field} is required"
        root = self._v2_vault_root()
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            return None, f"{field} must be inside vault root {root}"
        if not resolved.is_file():
            return None, f"file not found: {resolved}"
        return resolved, None

    async def _v2_lookups(self):
        profiles = await self.db.list_profiles()
        profile_map = {profile.id: profile for profile in profiles}
        return (
            RegistryContractLookup(),
            VaultProfileLookup(
                profile_map,
                plugin_command_names=self._plugin_command_names(),
            ),
            RegisteredEventLookup(),
        )

    def _v2_find_source(self, playbook_id: str) -> tuple[PlaybookSource | None, str | None]:
        matches: list[PlaybookSource] = []
        root = self._v2_vault_root()
        for directory in self._vault_playbook_dirs():
            for path in sorted(Path(directory).glob("*.md")):
                loaded = PlaybookSource.load(path, vault_root=root)
                if isinstance(loaded, PlaybookSource) and loaded.frontmatter.get("id") == playbook_id:
                    matches.append(loaded)
        if not matches:
            return None, f"no vault source declares playbook id {playbook_id!r}"
        if len(matches) > 1:
            paths = ", ".join(source.vault_path for source in matches)
            return None, f"multiple vault sources declare playbook id {playbook_id!r}: {paths}"
        return matches[0], None

    # ------------------------------------------------------------------
    # Package 2 compiler — review-only, never persists or activates
    # ------------------------------------------------------------------

    async def _cmd_playbook_v2_validate(self, args: dict) -> dict:
        if not self._v2_compiler_enabled():
            return {"success": False, "error": V2_COMPILER_DISABLED_ERROR}
        path, error = self._v2_resolve_vault_path(args.get("path"), "path")
        if error:
            return _command_error(error, field="path")
        assert path is not None
        try:
            definition = load_definition_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, ValidationError, DuplicateJsonKey, json.JSONDecodeError) as exc:
            return _command_error(f"invalid V2 artifact: {exc}", field="path")
        contracts, profiles, events = await self._v2_lookups()
        diagnostics = validate_definition(
            definition,
            inventory=None,
            contracts=contracts,
            profiles=profiles,
            events=events,
        )
        counts = _diagnostic_counts(diagnostics)
        clean = counts["error"] == 0 and counts["question"] == 0
        return {
            "success": clean,
            "artifact_sha256": artifact_sha256(definition) if clean else None,
            "counts": counts,
            "diagnostics": [_diagnostic_dict(diagnostic) for diagnostic in diagnostics],
        }

    async def _cmd_playbook_v2_propose(self, args: dict) -> dict:
        if not self._v2_compiler_enabled():
            return {"success": False, "error": V2_COMPILER_DISABLED_ERROR}
        playbook_id = _clean_str(args, "playbook_id")
        if not playbook_id:
            return _command_error("playbook_id is required", field="playbook_id")
        source, error = self._v2_find_source(playbook_id)
        if error:
            return _command_error(error, field="playbook_id")
        body_path, error = self._v2_resolve_vault_path(
            args.get("semantic_body_path"), "semantic_body_path"
        )
        if error:
            return _command_error(error, field="semantic_body_path")
        baseline: PlaybookDefinition | None = None
        baseline_arg = args.get("baseline_artifact_path")
        if baseline_arg:
            baseline_path, error = self._v2_resolve_vault_path(
                baseline_arg, "baseline_artifact_path"
            )
            if error:
                return _command_error(error, field="baseline_artifact_path")
            assert baseline_path is not None
            try:
                baseline = load_definition_json(baseline_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, ValidationError, DuplicateJsonKey, json.JSONDecodeError) as exc:
                return _command_error(
                    f"invalid baseline V2 artifact: {exc}", field="baseline_artifact_path"
                )
        assert source is not None and body_path is not None
        try:
            body = load_semantic_body_json(body_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, DuplicateSemanticKey, json.JSONDecodeError) as exc:
            return _command_error(f"invalid semantic body: {exc}", field="semantic_body_path")
        contracts, profiles, events = await self._v2_lookups()
        proposal = propose(
            source,
            body,
            baseline=baseline,
            contracts=contracts,
            profiles=profiles,
            events=events,
            version=(baseline.version + 1) if baseline is not None else 1,
        )
        counts = _diagnostic_counts(proposal.diagnostics)
        return {
            "success": proposal.artifact is not None,
            "activatable": proposal.activatable,
            "artifact_sha256": proposal.artifact_sha256,
            "source_digest": proposal.source_digest,
            "contract_fingerprint": proposal.contract_fingerprint,
            "compiler_build": proposal.compiler_build,
            "counts": counts,
            "diagnostics": [_diagnostic_dict(diagnostic) for diagnostic in proposal.diagnostics],
            "semantic_diff": asdict(proposal.semantic_diff) if proposal.semantic_diff else None,
            "artifact": (
                proposal.artifact.model_dump(mode="json", exclude_none=True)
                if proposal.artifact
                else None
            ),
        }

    async def _cmd_playbook_v2_shadow_compile(self, args: dict) -> dict:
        if not self._v2_compiler_enabled():
            return {"success": False, "error": V2_COMPILER_DISABLED_ERROR}
        scope = _clean_str(args, "scope")
        if scope and scope not in _VALID_SCOPES:
            return _command_error(
                f"Invalid scope {scope!r}. Valid: {', '.join(sorted(_VALID_SCOPES))}",
                field="scope",
            )
        root = self._v2_vault_root()
        sources: list[PlaybookSource] = []
        source_errors: list[dict[str, Any]] = []
        for directory in self._vault_playbook_dirs():
            for path in sorted(Path(directory).glob("*.md")):
                loaded = PlaybookSource.load(path, vault_root=root)
                if isinstance(loaded, SourceError):
                    source_errors.append({"path": str(path), "errors": list(loaded.errors)})
                    continue
                source_scope = str(loaded.frontmatter.get("scope") or "system")
                normalized_scope = "agent_type" if source_scope.startswith("agent-type") else source_scope.split(":", 1)[0]
                if scope and normalized_scope != scope:
                    continue
                sources.append(loaded)
        contracts, profiles, events = await self._v2_lookups()
        report = shadow_compile(
            sources,
            contracts=contracts,
            profiles=profiles,
            events=events,
        )
        rows = []
        for row in report.rows:
            counts = _diagnostic_counts(row.diagnostics)
            rows.append(
                {
                    "playbook_id": row.playbook_id,
                    "vault_path": row.vault_path,
                    "kind": row.kind,
                    "lowered": row.lowered,
                    "artifact_sha256": row.artifact_sha256,
                    "counts": counts,
                    "diagnostics": [_diagnostic_dict(d) for d in row.diagnostics],
                }
            )
        return {
            "success": not source_errors,
            "total": len(rows),
            "lowered": sum(row["lowered"] for row in rows),
            "clean": sum(
                row["counts"]["error"] == 0 and row["counts"]["question"] == 0
                for row in rows
            ),
            "rows": rows,
            "source_errors": source_errors,
        }

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

        Health is computed on demand from stored rows and the **live**
        registries, so a command contract or capability profile that has moved
        since the artifact was compiled reports ``stale_contract`` here with a
        reason naming the command or profile.  Nothing is written: persisting
        health stays the retention sweep\'s one-directional job.

        ``pending_event_count`` and ``running_count`` are the run-overlay
        counts Package 5 fills in with the rest of the projections; they are
        left at their DTO defaults rather than guessed at here.

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

        playbook_id = _clean_str(args, "playbook_id")

        if not self._v2_activation_storage_ready():
            return self._v2_storage_unavailable()

        from src.playbooks.activation import load_activation_health

        contracts, profiles, _events = await self._v2_lookups()
        records = await load_activation_health(self.db, contracts=contracts, profiles=profiles)

        activations = [
            record.as_dict()
            for record in records
            if (not playbook_id or record.playbook_id == playbook_id)
            and (not scope or record.scope == scope)
            and (not health or record.health.value == health)
        ]
        by_health: dict[str, int] = {}
        for activation in activations:
            by_health[activation["health"]] = by_health.get(activation["health"], 0) + 1
        return {
            "success": True,
            "activations": activations,
            "count": len(activations),
            "by_health": by_health,
        }

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
