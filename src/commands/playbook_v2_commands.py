"""Playbook V2 semantic-graph commands — graph, health, artifact listing,
diff, activation, pending events and run overlays.

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

Packages 1-4 now provide the strict artifact, contracts, activation store,
engine, run snapshots, pending events and receipts.  This module is their
operator-facing composition point; the three projectors remain pure and all I/O
stays here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from src.commands.principal import SERVER_OWNED_ARG_KEYS
from src.playbooks.authoring import PlaybookSource, SourceError
from src.playbooks.definition import (
    DuplicateJsonKey,
    PlaybookDefinition,
    artifact_sha256,
    canonical_bytes,
    load_definition_json,
    referenced_profile_ids,
    source_digest,
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
from src.playbooks.waits import PENDING_EVENT_DISPATCH_LEASE_SECONDS

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

_PENDING_EVENT_DISPATCH_RENEW_SECONDS = 60.0

#: A discard drops a real event that a playbook was entitled to see, so it
#: carries the same justification floor as a migration acknowledgement
#: (Package 6 §4.2, §5.5 T-16 assertion 10) — an empty waiver is not a waiver.
MIN_PENDING_EVENT_REASON_LENGTH = 20
PENDING_EVENT_REASON_TOO_SHORT_ERROR = (
    f"a discard reason must be at least {MIN_PENDING_EVENT_REASON_LENGTH} characters — "
    "an event dropped without a recorded reason is a silently lost event"
)

#: Why an ``automatic`` replay declined to consume a backlog.  Operators and
#: the dashboard read these verbatim, so keep them in step with
#: ``PlaybooksConfig.v2_pending_event_replay_on_activation``.
PENDING_EVENT_REPLAY_UNREVIEWED_REFUSAL = (
    "automatic replay is refused for a question_required activation — an "
    "unreviewed playbook may not auto-consume a backlog"
)
PENDING_EVENT_REPLAY_DISABLED_REFUSAL = (
    "automatic replay is refused for a disabled activation — a playbook that "
    "is not running may not consume a backlog"
)
PENDING_EVENT_REPLAY_BLOCKED_REFUSAL = (
    "automatic replay is refused because the activation was blocked"
)
PENDING_EVENT_REPLAY_UNREADABLE_REFUSAL = (
    "automatic replay is refused because the activation's health could not be "
    "read back after the write"
)


def _pending_event_replay_refusal(health: str, *, enabled: bool) -> str | None:
    """Fail-closed gate: which healths may auto-consume a backlog.

    Only a ``ready``, enabled activation may.  ``question_required`` gets its
    own message because the plan
    (``docs/superpowers/plans/2026-09-01-playbook-v2-migration-artifacts.md``
    §5.5 T-16) names that state specifically, and it is also the one
    ``PlaybooksConfig.validate(activation_healths=...)`` refuses in config.
    Every other non-``ready`` health is refused too — replaying a backlog into
    a stale, invalid or unavailable artifact is the same unreviewed
    consumption with a different label.
    """
    if health == "question_required":
        return PENDING_EVENT_REPLAY_UNREVIEWED_REFUSAL
    if not enabled or health == "disabled":
        return PENDING_EVENT_REPLAY_DISABLED_REFUSAL
    if health != "ready":
        return (
            f"automatic replay requires a ready activation; this one is "
            f"'{health}'"
        )
    return None


#: Reason code on the activation an operator gets back when the write landed
#: but the health read that follows it did not return the row.
ACTIVATION_HEALTH_UNREADABLE_CODE = "activation_health_unreadable"


def _unreadable_activation_payload(
    *,
    playbook_id: str,
    scope: str,
    scope_identifier: str,
    artifact_sha256: str,
    enabled: bool,
    activated_by: str,
) -> dict[str, Any]:
    """The ``ActivationStateDTO`` for a write whose health read came back empty.

    ``set_playbook_activation`` has already committed when this is reached, so
    the activation is live whatever the read that follows it says, and the
    response describes the row that was written.  What cannot be claimed is the
    *health*, which is exactly what that read computes — so it is reported as
    ``unavailable`` with a reason naming the playbook and the hash, rather than
    asserting the ``ready`` the write asked for.  That is also what keeps
    :meth:`_v2_replay_on_activation` fail-closed here: an unavailable health may
    not auto-consume a backlog.

    This mirrors the blocked path, which has always synthesised a payload when
    no record matched; the success path used to dereference the ``None`` and
    raise ``AttributeError`` at the operator instead.
    """
    from src.playbooks.activation import ActivationHealth, HealthReason

    return {
        "playbook_id": playbook_id,
        "scope": scope,
        "scope_identifier": scope_identifier or None,
        "enabled": enabled,
        "active_artifact_sha256": artifact_sha256,
        "health": ActivationHealth.UNAVAILABLE.value,
        "reasons": [
            HealthReason(
                ACTIVATION_HEALTH_UNREADABLE_CODE,
                (
                    f"Activation for playbook '{playbook_id}' at artifact "
                    f"'{artifact_sha256}' was written, but the activation health "
                    f"read did not return it"
                ),
                playbook_id,
            ).as_dict()
        ],
        "activated_by": activated_by,
    }


#: Semantic graph command names this mixin owns. Imported
#: by ``src/commands/handler.py`` (feature pause) and by the tests that pin the
#: registration surface.
PLAYBOOK_V2_COMMANDS: frozenset[str] = frozenset(
    {
        "playbook_v2_graph",
        "playbook_graph_layout_save",
        "playbook_activation_health",
        "playbook_activate",
        "playbook_artifact_diff",
        "playbook_pending_events",
        "playbook_pending_event_action",
        "playbook_run_overlay",
    }
)

#: Package 2's review-only compiler surface. Kept separate from the graph
#: projection and layout commands.
PLAYBOOK_V2_COMPILER_COMMANDS: frozenset[str] = frozenset(
    {
        "playbook_v2_validate",
        "playbook_v2_propose",
        "playbook_v2_shadow_compile",
    }
)

#: The activation chooser's read. Deliberately its own constant rather than a
#: member of ``PLAYBOOK_V2_COMMANDS`` because it answers a question
#: none of them do — *which artifacts could be activated*, including
#: the inactive candidates an operator diffs before activating one.
PLAYBOOK_V2_ARTIFACT_COMMANDS: frozenset[str] = frozenset({"playbook_artifacts"})

#: Operator adoption is a write, but not activation.  It stays separate from
#: the Package 2 compiler set so a playbook-compiler task can validate and
#: propose without gaining authority to persist reviewed bytes.
PLAYBOOK_V2_IMPORT_COMMANDS: frozenset[str] = frozenset({"playbook_v2_import"})

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

#: Default page size for the artifact chooser — enough versions to cover a
#: playbook's recent history without loading a whole retention window.
_ARTIFACT_LIST_DEFAULT_LIMIT = 50

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


def _artifact_summary(row: dict[str, Any], active_shas: set[str]) -> dict[str, Any]:
    """Project one ``playbook_artifacts`` row into ``PlaybookArtifactSummaryDTO``.

    Projected field-for-field rather than through ``ArtifactRef.from_row``: the
    dataclass rejects a row whose ``schema_generation`` this build no longer
    stores, and a chooser that hides an artifact is worse than one that shows an
    operator a candidate they cannot activate — ``playbook_activate`` is where
    that refusal belongs.
    """
    return {
        "artifact": {
            "playbook_id": row["playbook_id"],
            "artifact_sha256": row["artifact_sha256"],
            "schema_generation": int(row["schema_generation"]),
            "contract_fingerprint": row["contract_fingerprint"],
            "source_digest": row["source_digest"],
            "compiler_build": row["compiler_build"],
            "compiled_at": row.get("compiled_at"),
            "version": int(row["version"]),
        },
        "scope": row.get("scope") or "system",
        "scope_identifier": row.get("scope_identifier") or None,
        "size_bytes": int(row.get("size_bytes") or 0),
        "created_at": row.get("created_at"),
        "is_active": row["artifact_sha256"] in active_shas,
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


class _UniqueReviewLoader(yaml.SafeLoader):
    """YAML loader that refuses ambiguous duplicate review keys."""


def _construct_unique_review_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate review key {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueReviewLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_review_mapping,
)


class PlaybookV2CommandsMixin:
    """Playbook V2 semantic-graph command methods mixed into CommandHandler."""

    # ------------------------------------------------------------------
    # Gates
    # ------------------------------------------------------------------

    def _v2_api_enabled(self) -> bool:
        playbooks = getattr(self.config, "playbooks", None)
        return bool(getattr(playbooks, "enabled", False))

    def _v2_activation_writes_enabled(self) -> bool:
        playbooks = getattr(self.config, "playbooks", None)
        return bool(getattr(playbooks, "enabled", False))

    def _v2_activation_storage_ready(self) -> bool:
        """Whether activation health can actually be read from this build.

        Health needs only Package 3 state — ``playbook_activations`` plus the
        artifact rows and files — so it is the one projection that does not
        wait for the rest of ``_v2_storage_unavailable``\'s seam.  The gate is
        the storage flag itself plus the query the read path calls, so a
        database adapter without the V2 tables still reports the seam error
        rather than raising.
        """
        return hasattr(self.db, "list_playbook_activations") and hasattr(
            self.db, "get_playbook_artifact_row"
        )

    def _v2_artifact_storage_ready(self) -> bool:
        """Whether the stored artifact rows can actually be listed here.

        Same shape as :meth:`_v2_activation_storage_ready` and for the same
        reason: the artifact *rows* are Package 3 state, so listing them does
        not wait for the ``_v2_storage_unavailable`` seam, but a database
        adapter without the V2 tables must still report the seam error rather
        than raise.
        """
        return hasattr(self.db, "list_playbook_artifacts") and hasattr(
            self.db, "list_playbook_activations"
        )

    def _v2_compiler_enabled(self) -> bool:
        playbooks = getattr(self.config, "playbooks", None)
        return bool(getattr(playbooks, "enabled", False))

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

    def _v2_resolve_vault_directory(
        self, raw: Any, field: str
    ) -> tuple[Path | None, str | None]:
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
        if not resolved.is_dir():
            return None, f"directory not found: {resolved}"
        return resolved, None

    async def _v2_lookups(self):
        profiles = await self.db.list_profiles()
        profile_map = {profile.id: profile for profile in profiles}
        return (
            RegistryContractLookup(),
            VaultProfileLookup(
                profile_map,
                plugin_command_names=self._plugin_command_names(),
                intelligence_classes=self._v2_intelligence_classes(),
                llm_config=getattr(self.config, "llm", None),
            ),
            RegisteredEventLookup(),
        )

    def _v2_intelligence_classes(self):
        """The class snapshot the AI cards resolve a model against.

        Prefer the orchestrator's live registry so the graph and a session
        launch agree even after a vault edit; fall back to a fresh vault scan
        when no registry is mounted (tests, CLI-only handlers).
        """
        classes = self._live_intelligence_classes()
        if classes is not None:
            return classes
        from src.intelligence_classes import load_intelligence_classes

        try:
            return load_intelligence_classes(self.config.data_dir)
        except OSError:
            return None

    def _vault_playbook_dirs(self) -> list[Path]:
        """Directories where playbook Markdown sources may live in the vault.

        Mirrors the scope layout ``update_playbook_source`` writes to:
        ``system/playbooks``, ``agent-types/<id>/playbooks`` and
        ``projects/<pid>/playbooks``.
        """
        vault = self._v2_vault_root()
        dirs = [vault / "system" / "playbooks"]
        for parent in ("agent-types", "projects"):
            root = vault / parent
            if root.is_dir():
                for child in sorted(root.iterdir()):
                    playbooks = child / "playbooks"
                    if playbooks.is_dir():
                        dirs.append(playbooks)
        return dirs

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

    async def _cmd_playbook_v2_import(self, args: dict) -> dict:
        """Persist one mechanically validated V2 artifact bundle without activation.

        ``path`` names a directory inside the configured vault containing the
        Package 6 bundle layout: ``artifact.json``, ``artifact.sha256``,
        ``source.md`` and ``manifest.md``.  The metadata binds the exact playbook,
        artifact, source, profiles, and command-contract fingerprints; it does
        not impose a human approval policy.  The artifact is parsed
        strictly, must already be canonical, and is revalidated against the
        daemon's live command, profile and event registries before any write.

        The command writes the immutable content-addressed file and artifact
        row under one per-hash critical section.  If the write transaction
        fails, including while its context manager exits, a file created by
        this attempt is removed.  No activation row is read or written.

        Args:
            path: Vault-relative or absolute path to the artifact bundle
                directory.
        """
        from src.commands.principal import PrincipalKind, current_principal

        principal = current_principal()
        if principal is not None and not (
            principal.kind is PrincipalKind.LOCAL or principal.elevated
        ):
            return {
                "success": False,
                "error": "out of scope: playbook_v2_import requires elevated capability scope",
            }
        if not self._v2_api_enabled():
            return {"success": False, "error": V2_API_DISABLED_ERROR}
        if not self._v2_activation_writes_enabled():
            return {
                "success": False,
                "error": (
                    "playbook v2 artifact import is disabled "
                    "(playbooks.v2_activation_writes=false)"
                ),
            }
        if not self._v2_storage_ready(
            "artifact_hash_lock",
            "get_playbook_artifact_row",
            "upsert_playbook_artifact",
        ):
            return {"success": False, **self._v2_storage_unavailable()}

        directory, error = self._v2_resolve_vault_directory(args.get("path"), "path")
        if error:
            return {"success": False, "error": error}
        assert directory is not None

        required: dict[str, Path] = {}
        missing: list[str] = []
        vault_root = self._v2_vault_root()
        for name in ("artifact.json", "artifact.sha256", "source.md", "manifest.md"):
            try:
                resolved = (directory / name).resolve()
                resolved.relative_to(vault_root)
            except (OSError, ValueError):
                return {
                    "success": False,
                    "error": f"{name} must be inside vault root {vault_root}",
                }
            if not resolved.is_file():
                missing.append(name)
            required[name] = resolved
        if missing:
            return {
                "success": False,
                "error": f"artifact bundle is incomplete; missing {', '.join(missing)}",
            }

        try:
            artifact_bytes = required["artifact.json"].read_bytes()
            definition = load_definition_json(artifact_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            return {"success": False, "error": f"invalid artifact: {exc}"}
        if canonical_bytes(definition) != artifact_bytes:
            return {
                "success": False,
                "error": "artifact.json is not canonical PlaybookDefinition bytes",
            }

        actual_sha = "sha256:" + hashlib.sha256(artifact_bytes).hexdigest()
        try:
            sha_lines = required["artifact.sha256"].read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return {"success": False, "error": f"artifact.sha256 is unreadable: {exc}"}
        if len(sha_lines) != 1 or sha_lines[0].strip() != sha_lines[0]:
            return {
                "success": False,
                "error": "artifact.sha256 must contain exactly one full digest with no whitespace",
            }
        recorded_sha = sha_lines[0]
        invalid = _validate_sha(recorded_sha, "artifact_sha256")
        if invalid:
            return {"success": False, "error": invalid}
        if recorded_sha != actual_sha:
            return {
                "success": False,
                "error": "artifact_sha256 does not match artifact.json bytes",
            }

        try:
            manifest_text = required["manifest.md"].read_text(encoding="utf-8")
            if not manifest_text.startswith("---\n"):
                raise ValueError("manifest.md has no YAML frontmatter")
            end = manifest_text.find("\n---\n", 4)
            if end < 0:
                raise ValueError("manifest.md has no closing YAML frontmatter marker")
            manifest = yaml.load(manifest_text[4:end], Loader=_UniqueReviewLoader)
            if not isinstance(manifest, dict):
                raise TypeError("manifest.md frontmatter is not a mapping")
        except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
            return {"success": False, "error": f"artifact manifest is invalid: {exc}"}


        expected = {
            "playbook_id": definition.id,
            "artifact_sha256": actual_sha,
            "source_sha256": definition.source_hash,
            "contract_fingerprint": definition.contract_fingerprint(),
        }
        for field, value in expected.items():
            if manifest.get(field) != value:
                return {
                    "success": False,
                    "error": f"manifest.md {field} does not match the artifact",
                }
        if directory.name != definition.id:
            return {
                "success": False,
                "error": "artifact bundle directory name does not match playbook_id",
            }
        manifest_profiles = manifest.get("profiles_referenced")
        if not isinstance(manifest_profiles, list) or sorted(manifest_profiles) != list(
            referenced_profile_ids(definition)
        ):
            return {
                "success": False,
                "error": "manifest.md profiles_referenced does not match the artifact",
            }
        try:
            source_text = required["source.md"].read_text(encoding="utf-8")
        except OSError as exc:
            return {"success": False, "error": f"source.md is unreadable: {exc}"}
        if source_digest(source_text) != definition.source_hash:
            return {
                "success": False,
                "error": "source_sha256 does not match source.md bytes",
            }

        contracts, profiles, events = await self._v2_lookups()
        diagnostics = validate_definition(
            definition,
            inventory=None,
            contracts=contracts,
            profiles=profiles,
            events=events,
        )
        diagnostic_rows = [_diagnostic_dict(diagnostic) for diagnostic in diagnostics]
        if any(diagnostic.severity in {"error", "question"} for diagnostic in diagnostics):
            return {
                "success": False,
                "error": "artifact does not validate against the live registries",
                "diagnostics": diagnostic_rows,
            }

        from src.playbooks.activation import profile_fingerprint

        aggregate_profile_fingerprint = profile_fingerprint(
            dict(definition.compiled_against.profiles)
        )
        scope, scope_identifier = self._v2_scope(definition)
        store = self._v2_engine().services.artifact_store
        validation = json.dumps(
            {
                "errors": [],
                "diagnostics": diagnostic_rows,
                "validated_bundle": True,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        remove_file_on_failure = False
        try:
            async with self.db.artifact_hash_lock([actual_sha]) as conn:
                existing_row = await self.db.get_playbook_artifact_row(actual_sha, conn=conn)
                file_existed = store.exists(actual_sha)
                # Set this before put(): it may publish the destination and then
                # fail while making the containing directory durable.  An
                # existing row or file belongs to an earlier attempt and must
                # survive rollback of this one.
                remove_file_on_failure = existing_row is None and not file_existed
                ref = store.put(
                    definition,
                    source_digest=definition.source_hash,
                    contract_fingerprint=definition.contract_fingerprint(),
                    profile_fingerprint=aggregate_profile_fingerprint,
                    compiler_build=definition.compiler_build or "unknown",
                    version=definition.version,
                )
                await self.db.upsert_playbook_artifact(
                    ref,
                    scope=scope,
                    scope_identifier=scope_identifier,
                    profile_fingerprint=aggregate_profile_fingerprint,
                    path=store.path_for(ref.artifact_sha256),
                    size_bytes=len(artifact_bytes),
                    validation=validation,
                    conn=conn,
                )
        except BaseException as exc:
            # This handler deliberately surrounds the whole async-with.  A
            # transaction commit happens in __aexit__, after the suite above,
            # and cancellation there must receive the same compensation as a
            # failed upsert without converting CancelledError into a result.
            if remove_file_on_failure:
                store.delete(actual_sha)
            if not isinstance(exc, Exception):
                raise
            logger.warning("could not import reviewed V2 artifact %s", actual_sha, exc_info=True)
            return {"success": False, "error": f"artifact import failed: {exc}"}

        return {
            "success": True,
            "playbook_id": definition.id,
            "artifact_sha256": ref.artifact_sha256,
            "scope": scope,
            "scope_identifier": scope_identifier or None,
            "schema_version": definition.schema_version,
            "version": definition.version,
            "source_sha256": definition.source_hash,
            "contract_fingerprint": definition.contract_fingerprint(),
            "activated": False,
            "diagnostics": diagnostic_rows,
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
        """Compatibility response when the independently gated store is off."""
        return {"error": V2_STORAGE_UNAVAILABLE_ERROR}

    def _v2_storage_ready(self, *methods: str) -> bool:
        return all(
            hasattr(self.db, method) for method in methods
        )

    async def _v2_health_records(self):
        from src.playbooks.activation import load_activation_health

        contracts, profiles, _events = await self._v2_lookups()
        records = await load_activation_health(self.db, contracts=contracts, profiles=profiles)
        return records, contracts, profiles

    @staticmethod
    def _v2_activation_for(records, playbook_id: str, sha: str | None = None):
        matches = [record for record in records if record.playbook_id == playbook_id]
        if sha:
            exact = [record for record in matches if record.active_artifact_sha256 == sha]
            if exact:
                return exact[0]
        return matches[0] if matches else None

    async def _v2_activation_payload(self, record) -> dict[str, Any]:
        payload = record.as_dict()
        if hasattr(self.db, "count_pending_events"):
            payload["pending_event_count"] = await self.db.count_pending_events(
                record.playbook_id, reasons=sorted(_VALID_PENDING_REASONS)
            )
        if hasattr(self.db, "count_active_runs"):
            payload["running_count"] = await self.db.count_active_runs(record.playbook_id)
        return payload

    async def _v2_load_artifact(self, sha: str, playbook_id: str | None = None):
        ref = await self.db.get_playbook_artifact(sha)
        if ref is None:
            return None, None, f"Playbook artifact '{sha}' not found"
        if playbook_id is not None and ref.playbook_id != playbook_id:
            return None, None, f"Artifact '{sha}' does not belong to playbook '{playbook_id}'"
        try:
            definition = self._v2_engine().services.artifact_store.load(sha)
        except Exception as exc:
            logger.warning("could not load V2 artifact %s", sha, exc_info=True)
            return None, None, f"Playbook artifact '{sha}' is unavailable: {exc}"
        return ref, definition, None

    @staticmethod
    def _v2_scope(definition: PlaybookDefinition) -> tuple[str, str]:
        scope = definition.scope
        identifier = getattr(scope, "project_id", None) or getattr(scope, "agent_type", None) or ""
        return scope.type, identifier

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

        if not self._v2_storage_ready(
            "list_playbook_activations", "get_playbook_artifact", "get_playbook_artifact_row"
        ):
            return self._v2_storage_unavailable()
        records, contracts, profiles = await self._v2_health_records()
        activation = self._v2_activation_for(records, playbook_id, artifact_sha256 or None)
        selected_sha = artifact_sha256 or (
            activation.active_artifact_sha256 if activation is not None else None
        )
        if not selected_sha:
            return {"error": f"No active V2 artifact for playbook '{playbook_id}'"}
        ref, definition, error = await self._v2_load_artifact(selected_sha, playbook_id)
        if error:
            return {"error": error}
        from src.playbooks.graph_projection import project_graph

        response = project_graph(
            definition,
            ref,
            await self._v2_activation_payload(activation) if activation is not None else None,
            event_type=_clean_str(args, "event_type") or None,
            contracts=contracts,
            profiles=profiles,
            direction=direction,
            layout_overrides=(
                self._v2_engine().services.artifact_store.load_layout(selected_sha)
                if hasattr(self._v2_engine().services.artifact_store, "load_layout")
                else {}
            ),
        )
        if args.get("include_advanced", True) is False:
            for node in response["nodes"]:
                node["advanced"]["typed_step"] = {}
        return response

    async def _cmd_playbook_graph_layout_save(self, args: dict) -> dict:
        """Persist user-arranged node coordinates for one immutable artifact."""
        if not self._v2_api_enabled():
            return {"error": V2_API_DISABLED_ERROR}
        playbook_id = _clean_str(args, "playbook_id")
        if not playbook_id:
            return {"error": "playbook_id is required"}
        artifact_sha256 = _clean_str(args, "artifact_sha256")
        invalid = _validate_sha(artifact_sha256, "artifact_sha256")
        if invalid:
            return {"error": invalid}
        if not self._v2_storage_ready("get_playbook_artifact"):
            return self._v2_storage_unavailable()
        _ref, definition, error = await self._v2_load_artifact(artifact_sha256, playbook_id)
        if error:
            return {"error": error}
        raw_positions = args.get("positions")
        if not isinstance(raw_positions, dict) or not raw_positions:
            return {"error": "positions must be a non-empty object"}
        positions: dict[str, dict[str, int]] = {}
        for step_id, raw in raw_positions.items():
            if step_id not in definition.steps:
                return {"error": f"Unknown step_id '{step_id}'"}
            if not isinstance(raw, dict):
                return {"error": f"Position for '{step_id}' must contain integer x and y"}
            x, y = raw.get("x"), raw.get("y")
            if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, int) or not isinstance(y, int):
                return {"error": f"Position for '{step_id}' must contain integer x and y"}
            positions[step_id] = {"x": x, "y": y}
        store = self._v2_engine().services.artifact_store
        positions = {**store.load_layout(artifact_sha256), **positions}
        store.save_layout(artifact_sha256, positions)
        return {
            "success": True,
            "playbook_id": playbook_id,
            "artifact_sha256": artifact_sha256,
            "positions": positions,
        }

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

        matching = [
            record
            for record in records
            if (not playbook_id or record.playbook_id == playbook_id)
            and (not scope or record.scope == scope)
            and (not health or record.health.value == health)
        ]
        activations = [await self._v2_activation_payload(record) for record in matching]
        by_health: dict[str, int] = {}
        for activation in activations:
            by_health[activation["health"]] = by_health.get(activation["health"], 0) + 1
        return {
            "success": True,
            "activations": activations,
            "count": len(activations),
            "by_health": by_health,
        }

    async def _cmd_playbook_artifacts(self, args: dict) -> dict:
        """List the stored artifacts of one playbook, active flagged.

        The chooser behind the activation review.  ``playbook_activation_health``
        names only the artifact a scope has *already* activated, so without this
        read an operator has no way to name the newly compiled candidate they
        want to diff and activate — the dashboard would be left diffing the
        active artifact against itself.

        Read-only, and it never loads an artifact's bytes: the rows carry the
        whole identity the chooser shows, and the diff and graph commands do the
        loading once a hash has been picked.

        Args:
            playbook_id: Required — the playbook whose artifacts to list.
            limit: Max artifacts to return, newest version first. Default: 50.
        """
        if not self._v2_api_enabled():
            return {"error": V2_API_DISABLED_ERROR}

        playbook_id = _clean_str(args, "playbook_id")
        if not playbook_id:
            return {"error": "playbook_id is required"}

        raw_limit = args.get("limit", _ARTIFACT_LIST_DEFAULT_LIMIT)
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return {"error": "limit must be an integer"}
        if limit < 1:
            return {"error": "limit must be >= 1"}

        if not self._v2_artifact_storage_ready():
            return self._v2_storage_unavailable()

        rows = await self.db.list_playbook_artifacts(playbook_id, limit=limit)
        activations = [
            row
            for row in await self.db.list_playbook_activations()
            if row.get("playbook_id") == playbook_id
        ]
        # One playbook can be activated in several scopes, so "active" is a set
        # rather than a single hash.  ``active_artifact_sha256`` reports the
        # most recently updated one, which is the row the health read shows
        # first, and ``is_active`` stays true for every scope's choice.
        active_shas = {
            row["active_artifact_sha256"]
            for row in activations
            if row.get("active_artifact_sha256")
        }
        active_artifact_sha256 = next(
            (
                row["active_artifact_sha256"]
                for row in sorted(
                    activations, key=lambda row: row.get("updated_at") or 0, reverse=True
                )
                if row.get("active_artifact_sha256")
            ),
            None,
        )
        return {
            "success": True,
            "playbook_id": playbook_id,
            "artifacts": [_artifact_summary(row, active_shas) for row in rows],
            "count": len(rows),
            "active_artifact_sha256": active_artifact_sha256,
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

        if not self._v2_storage_ready(
            "list_playbook_activations", "get_playbook_artifact", "get_playbook_artifact_row"
        ):
            return self._v2_storage_unavailable()
        records, contracts, profiles = await self._v2_health_records()
        activation = self._v2_activation_for(records, playbook_id)
        base_sha256 = base_sha256 or (
            activation.active_artifact_sha256 if activation is not None else None
        )
        target_ref, target, error = await self._v2_load_artifact(target_sha256, playbook_id)
        if error:
            return {"error": error}
        base_ref = base = None
        if base_sha256:
            base_ref, base, error = await self._v2_load_artifact(base_sha256, playbook_id)
            if error:
                return {"error": error}
        from src.playbooks.artifact_diff import diff_artifacts

        return diff_artifacts(
            base,
            target,
            base_ref=base_ref,
            target_ref=target_ref,
            contracts=contracts,
            profiles=profiles,
        )

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

        if not self._v2_storage_ready("list_pending_events"):
            return self._v2_storage_unavailable()
        rows = await self.db.list_pending_events(
            playbook_id=_clean_str(args, "playbook_id") or None,
            reasons=[reason] if reason else sorted(_VALID_PENDING_REASONS),
            include_resolved=False,
            limit=limit,
        )
        events = []
        for row in rows:
            from src.playbooks.run_overlay import redact_event

            safe_event = redact_event(dict(row.get("event") or {}), row["event_type"])
            events.append(
                {
                    "pending_event_id": row["pending_event_id"],
                    "playbook_id": row["playbook_id"],
                    "event_type": row["event_type"],
                    "event": safe_event,
                    "received_at": row["received_at"],
                    "reason": row["reason"],
                    "attempts": row.get("attempts", 0),
                    "last_error": row.get("last_error"),
                    "expires_at": row.get("expires_at"),
                }
            )
        by_reason: dict[str, int] = {}
        for event in events:
            by_reason[event["reason"]] = by_reason.get(event["reason"], 0) + 1
        return {
            "success": True,
            "events": events,
            "count": len(events),
            "oldest_received_at": events[0]["received_at"] if events else None,
            "by_reason": by_reason,
        }

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

        if not self._v2_storage_ready(
            "load_run",
            "list_receipts",
            "count_receipts",
            "get_playbook_artifact",
            "list_playbook_activations",
        ):
            return self._v2_storage_unavailable()
        run = await self.db.load_run(run_id)
        if run is None:
            return {"error": f"Playbook run '{run_id}' not found"}
        ref, definition, error = await self._v2_load_artifact(run.artifact_sha256, run.playbook_id)
        if error:
            return {"error": error}
        visible_receipt_kinds = ("step", "interrupted", "operator_decision")
        receipt_total = await self.db.count_receipts(
            run_id, receipt_kinds=visible_receipt_kinds
        )
        receipts = await self.db.list_receipts(
            run_id,
            limit=receipt_limit,
            offset=max(0, receipt_total - receipt_limit),
            receipt_kinds=visible_receipt_kinds,
        )
        records = await self.db.list_playbook_activations(enabled_only=False)
        active_sha = next(
            (
                row.get("active_artifact_sha256")
                for row in records
                if row.get("playbook_id") == run.playbook_id and row.get("enabled")
            ),
            None,
        )
        from src.commands.contracts import CONTRACTS
        from src.playbooks.run_overlay import project_overlay

        return project_overlay(
            run,
            receipts,
            definition,
            ref,
            active_sha256=active_sha,
            contracts=CONTRACTS,
            receipt_limit=receipt_limit,
            receipt_total=receipt_total,
        )

    # ------------------------------------------------------------------
    # Operator writes — separately feature-gated (child plan §7.3, §8)
    # ------------------------------------------------------------------

    async def _cmd_playbook_activate(self, args: dict) -> dict:
        """Activate one validated artifact hash for a playbook.

        Activation is an explicit database operation against a reviewed hash;
        compilation never activates.  The command refuses unless
        ``playbooks.v2_activation_writes`` is on, the target artifact's health
        is not ``invalid``, and either the diff against the currently active
        artifact carries no executable change or the caller passed
        ``acknowledge_diff`` equal to ``artifact_sha256`` — the literal hash, so
        an acknowledgement cannot be replayed against a different artifact.
        A project-scoped artifact additionally requires the caller's
        server-derived project to match and persists that principal as review
        evidence for the exact activated hash.

        Args:
            playbook_id: Required — the playbook to activate against.
            artifact_sha256: Required — the validated artifact hash.
            enabled: Whether the activation is enabled. Default: ``True``.
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

        if not self._v2_storage_ready(
            "list_playbook_activations",
            "get_playbook_artifact",
            "get_playbook_artifact_row",
            "set_playbook_activation",
        ):
            return self._v2_storage_unavailable()
        target_ref, target, error = await self._v2_load_artifact(artifact_sha256, playbook_id)
        if error:
            return {"error": error}
        scope, scope_identifier = self._v2_scope(target)
        from src.commands.principal import PrincipalKind, current_principal

        principal = current_principal()
        global_operator = principal is None or principal.kind is PrincipalKind.LOCAL or (
            principal.kind is PrincipalKind.SESSION
            and principal.elevated
            and principal.project_id is None
        )
        if (
            scope == "project"
            and not global_operator
            and principal.project_id != scope_identifier
        ):
            return {"error": "out of scope: project_id mismatch"}
        records, contracts, profiles = await self._v2_health_records()
        _contracts, _profiles, events = await self._v2_lookups()
        current = self._v2_activation_for(records, playbook_id)
        current_sha = current.active_artifact_sha256 if current is not None else None
        base_ref = base = None
        if current_sha:
            base_ref, base, error = await self._v2_load_artifact(current_sha, playbook_id)
            if error:
                return {"error": error}
        from src.playbooks.artifact_diff import diff_artifacts

        diff = diff_artifacts(
            base,
            target,
            base_ref=base_ref,
            target_ref=target_ref,
            contracts=contracts,
            profiles=profiles,
        )
        blockers = list(diff["activation_blockers"])
        validation = validate_definition(
            target,
            inventory=None,
            contracts=contracts,
            profiles=profiles,
            events=events,
        )
        blockers.extend(
            diagnostic.message
            for diagnostic in validation
            if diagnostic.severity in {"error", "question"}
        )
        if blockers:
            activation = (
                await self._v2_activation_payload(current)
                if current is not None
                else {
                    "playbook_id": playbook_id,
                    "scope": target.scope.type,
                    "scope_identifier": self._v2_scope(target)[1] or None,
                    "enabled": False,
                    "active_artifact_sha256": None,
                    "health": "disabled",
                    "reasons": [],
                }
            )
            return {
                "success": True,
                "activation": activation,
                "previous_artifact_sha256": current_sha,
                "changed": False,
                "blocked": True,
                "blockers": blockers,
                "pending_event_replay": self._v2_replay_report(
                    refused_reason=(
                        PENDING_EVENT_REPLAY_BLOCKED_REFUSAL
                        if self._v2_replay_policy() == "automatic"
                        else None
                    )
                ),
            }
        actor = principal.describe() if principal is not None else "local"
        enabled = args.get("enabled", True)
        if not isinstance(enabled, bool):
            return {"error": "enabled must be a boolean"}
        await self.db.set_playbook_activation(
            playbook_id=playbook_id,
            scope=scope,
            scope_identifier=scope_identifier,
            artifact_sha256=artifact_sha256,
            enabled=enabled,
            activated_by=actor,
            health="ready" if enabled else "disabled",
            reasons="[]",
        )
        manager = getattr(getattr(self, "orchestrator", None), "playbook_manager", None)
        if manager is not None:
            from src.playbooks.routing import refresh_routing_activation_snapshot

            await refresh_routing_activation_snapshot(manager, self.db)
        refreshed, _contracts, _profiles = await self._v2_health_records()
        activation = self._v2_activation_for(refreshed, playbook_id, artifact_sha256)
        replay = await self._v2_replay_on_activation(playbook_id, activation)
        return {
            "success": True,
            "activation": (
                activation.as_dict()
                if activation is not None
                else _unreadable_activation_payload(
                    playbook_id=playbook_id,
                    scope=scope,
                    scope_identifier=scope_identifier,
                    artifact_sha256=artifact_sha256,
                    enabled=enabled,
                    activated_by=actor,
                )
            ),
            "previous_artifact_sha256": current_sha,
            "changed": current_sha != artifact_sha256 or bool(current and current.enabled) != enabled,
            "blocked": False,
            "blockers": [],
            "pending_event_replay": replay,
        }

    async def _cmd_set_playbook_enabled(self, args: dict) -> dict:
        """Pause or resume a playbook's activation without changing its artifact.

        ``enabled=false`` stops trigger events from starting new runs of the
        active artifact; in-flight runs are not cancelled.  ``enabled=true``
        resumes the same artifact.  Scope, artifact hash and the activating
        principal are preserved; only ``enabled`` and the derived health change,
        written exactly as ``playbook_activate`` writes them.

        Args:
            playbook_id: Required — the playbook whose activation to toggle.
            enabled: Required — ``true`` to resume, ``false`` to pause.
        """
        if not self._v2_api_enabled():
            return {"error": V2_API_DISABLED_ERROR}
        if not self._v2_activation_writes_enabled():
            return {"error": V2_WRITES_DISABLED_ERROR}
        playbook_id = _clean_str(args, "playbook_id")
        if not playbook_id:
            return {"error": "playbook_id is required"}
        if "enabled" not in args:
            return {"error": "enabled is required"}
        enabled = args["enabled"]
        if not isinstance(enabled, bool):
            return {"error": "enabled must be a boolean"}
        if not self._v2_storage_ready("list_playbook_activations", "set_playbook_activation"):
            return self._v2_storage_unavailable()
        rows = [
            dict(row)
            for row in await self.db.list_playbook_activations(enabled_only=False)
            if dict(row).get("playbook_id") == playbook_id
        ]
        if not rows:
            return {"error": f"Playbook '{playbook_id}' not found"}
        row = rows[0]
        artifact_sha256 = row.get("active_artifact_sha256") or None
        if enabled and not artifact_sha256:
            return {
                "error": (
                    f"Playbook '{playbook_id}' has no active artifact to enable; "
                    "activate one with playbook_activate"
                )
            }
        if bool(row.get("enabled")) == enabled:
            return {"success": True, "playbook_id": playbook_id, "enabled": enabled, "noop": True}
        await self.db.set_playbook_activation(
            playbook_id=playbook_id,
            scope=row["scope"],
            scope_identifier=row.get("scope_identifier") or "",
            artifact_sha256=artifact_sha256,
            enabled=enabled,
            activated_by=row.get("activated_by"),
            health="ready" if enabled else "disabled",
            reasons="[]",
        )
        manager = getattr(getattr(self, "orchestrator", None), "playbook_manager", None)
        if manager is not None:
            from src.playbooks.routing import refresh_routing_activation_snapshot

            await refresh_routing_activation_snapshot(manager, self.db)
        return {"success": True, "playbook_id": playbook_id, "enabled": enabled, "noop": False}

    def _v2_replay_policy(self) -> str:
        """``manual`` or ``automatic`` — the configured activation replay policy."""
        playbooks = getattr(self.config, "playbooks", None)
        policy = getattr(playbooks, "v2_pending_event_replay_on_activation", "manual")
        return str(policy or "manual")

    def _v2_replay_report(
        self, *, replayed: bool = False, refused_reason: str | None = None, **extra: Any
    ) -> dict[str, Any]:
        report: dict[str, Any] = {
            "policy": self._v2_replay_policy(),
            "replayed": replayed,
            "refused_reason": refused_reason,
            "considered": 0,
            "dispatched_run_ids": [],
            "skipped": [],
            "errors": [],
        }
        report.update(extra)
        return report

    async def _v2_replay_on_activation(self, playbook_id: str, activation: Any) -> dict[str, Any]:
        """Consume the backlog held behind a freshly activated artifact.

        ``playbooks.v2_pending_event_replay_on_activation`` decides whether
        activating an artifact also drains the events held behind it.
        ``manual`` (the default) does nothing here and leaves the backlog to
        ``playbook_pending_event_action``; ``automatic`` replays it, and the
        report says which happened, so an operator never has to infer the
        policy from an empty queue.

        The replay is deliberately *not* a shortcut: every held row goes
        through :meth:`_v2_replay_held_event`, so the artifact that was just
        activated re-runs its own rule matching and ``when`` guards, the held
        payload's server-owned keys are stripped, and a failed dispatch
        restores the row instead of consuming it.  Rows are taken oldest
        first — arrival order is replay order (§6.6) — and bounded by the same
        ``v2_max_pending_events_per_playbook`` quota that bounds the queue, so
        an activation cannot become an unbounded dispatch storm.

        Fail-closed: the gate reads the health recomputed *after* the write,
        not the value the write asked for, and refuses anything that is not a
        ready, enabled activation — including the case where that read does
        not return the row at all and there is no recomputed health to gate
        on.  A refusal is not an activation failure — the artifact is live
        either way — so it is reported rather than raised, and the backlog
        stays operable by hand.
        """
        if self._v2_replay_policy() != "automatic":
            return self._v2_replay_report()

        if activation is None:
            # The write committed but the health read did not return the row,
            # so there is no recomputed health to gate on.  Refusing by its
            # own name keeps the report honest: the generic disabled refusal
            # would tell an operator the playbook is not running, which is not
            # what happened.
            return self._v2_replay_report(
                refused_reason=PENDING_EVENT_REPLAY_UNREADABLE_REFUSAL
            )
        refusal = _pending_event_replay_refusal(
            activation.health.value, enabled=bool(activation.enabled)
        )
        if refusal:
            return self._v2_replay_report(refused_reason=refusal)

        if not self._v2_storage_ready(
            "list_pending_events",
            "claim_pending_event_dispatch",
            "renew_pending_event_dispatch_claim",
            "finalize_pending_event_dispatch",
            "record_pending_event_dispatch_failure",
        ):
            return self._v2_replay_report(refused_reason=V2_STORAGE_UNAVAILABLE_ERROR)

        playbooks = getattr(self.config, "playbooks", None)
        limit = int(getattr(playbooks, "v2_max_pending_events_per_playbook", 1000) or 1000)
        rows = await self.db.list_pending_events(
            playbook_id=playbook_id,
            reasons=sorted(_VALID_PENDING_REASONS),
            include_resolved=False,
            limit=max(1, limit),
        )

        from src.commands.principal import ExecutionPrincipal, current_principal

        principal = current_principal() or ExecutionPrincipal.service("playbook-pending-event")
        actor = principal.describe()
        dispatched: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        for row in rows:
            claimed, run_ids, error = await self._v2_replay_held_event(row, principal, actor)
            if not claimed:
                skipped.append(row["pending_event_id"])
                continue
            if error:
                errors.append(f"{row['pending_event_id']}: {error}")
                continue
            dispatched.extend(run_ids)
        return self._v2_replay_report(
            replayed=True,
            considered=len(rows),
            dispatched_run_ids=dispatched,
            skipped=skipped,
            errors=errors,
        )

    @staticmethod
    def _v2_replayable_event(event: Any) -> dict[str, Any]:
        """The held payload with the keys the server owns removed.

        Held events are untrusted input stored as received (Package 6 §4.3):
        they are never re-signed or re-attributed, and the replaying
        principal is this request's, never the payload's.  Stripping happens
        here rather than at retention so the stored row stays a faithful
        record of what arrived.
        """
        return {
            key: value
            for key, value in dict(event or {}).items()
            if key not in SERVER_OWNED_ARG_KEYS
        }

    async def _v2_dispatch_pending_event(self, row, principal, claim_token: str):
        """Dispatch while renewing the durable claim that excludes other operators."""
        pending_event_id = row["pending_event_id"]
        stable_dispatch_id = hashlib.sha256(
            f"v2-pending|{pending_event_id}".encode()
        ).hexdigest()[:12]
        dispatch = asyncio.create_task(
            self._v2_engine().dispatch_event(
                self._v2_replayable_event(row["event"]),
                principal,
                playbook_ids=[row["playbook_id"]],
                dispatch_id=stable_dispatch_id,
            )
        )
        try:
            while True:
                done, _pending = await asyncio.wait(
                    {dispatch}, timeout=_PENDING_EVENT_DISPATCH_RENEW_SECONDS
                )
                if dispatch in done:
                    return await dispatch
                renewed = await self.db.renew_pending_event_dispatch_claim(
                    pending_event_id,
                    claim_token=claim_token,
                    now=time.time(),
                )
                if not renewed:
                    raise RuntimeError("pending event dispatch claim was lost")
        finally:
            if not dispatch.done():
                dispatch.cancel()
                with suppress(BaseException):
                    await dispatch

    async def _v2_replay_held_event(
        self, row: dict[str, Any], principal: Any, actor: str
    ) -> tuple[bool, tuple[str, ...], str | None]:
        """Claim one held row, replay it, and release the claim if it fails.

        The single replay primitive behind both the operator's
        ``playbook_pending_event_action dispatch`` and the automatic
        replay ``playbook_activate`` performs, so the two paths cannot
        drift: a replay is always a fresh dispatch through the durable
        claim, and a failed dispatch always restores the row instead of
        consuming it (Package 6 §4.3).

        Returns ``(claimed, run_ids, error)``.  ``claimed`` is ``False``
        when another operator already holds the row and nothing was
        attempted — the caller reports that as skipped rather than as a
        failure.  Cancellation and non-``Exception`` failures propagate
        after the claim has been released.
        """
        event_id = row["pending_event_id"]
        claimed_at = time.time()
        claim_token = await self.db.claim_pending_event_dispatch(
            event_id,
            claimed_by=actor,
            now=claimed_at,
            stale_before=claimed_at - PENDING_EVENT_DISPATCH_LEASE_SECONDS,
        )
        if claim_token is None:
            return False, (), None
        try:
            result = await self._v2_dispatch_pending_event(row, principal, claim_token)
            finalized = await self.db.finalize_pending_event_dispatch(
                event_id,
                claim_token=claim_token,
                resolved_by=actor,
                now=time.time(),
            )
            if not finalized:
                raise RuntimeError("pending event dispatch claim was lost before finalization")
            return True, tuple(result.run_ids), None
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            error = "dispatch cancelled" if cancelled else str(exc)
            if not cancelled:
                logger.warning("pending V2 event %s dispatch failed", event_id, exc_info=True)
            try:
                cancelled_during_recovery = None
                recovery = asyncio.create_task(
                    self.db.record_pending_event_dispatch_failure(
                        event_id,
                        claim_token=claim_token,
                        error=error,
                    )
                )
                restored = await asyncio.shield(recovery)
            except asyncio.CancelledError as recovery_cancel:
                cancelled_during_recovery = recovery_cancel
                restored = await recovery
            except Exception as restore_exc:
                logger.exception(
                    "pending V2 event %s could not restore its failed dispatch",
                    event_id,
                )
                error = f"{error}; failed to restore pending event: {restore_exc}"
            else:
                if not restored:
                    error = f"{error}; failed dispatch no longer owns pending event claim"
            if cancelled_during_recovery is not None:
                raise cancelled_during_recovery
            if not isinstance(exc, Exception):
                raise
            return True, (), error

    async def _cmd_playbook_pending_event_action(self, args: dict) -> dict:
        """Dispatch or discard held pending events.

        ``dispatch`` re-enters the engine's own event dispatch with the
        server-derived principal of this request — it never re-implements
        matching and never adopts a principal from the stored event.
        ``discard`` records the resolution without dispatching.

        A replay is a *fresh* dispatch: the current activation's rule
        matching and guards are re-evaluated from scratch, and the held
        payload's server-owned keys are stripped before it re-enters the
        engine, so a held event can never carry a principal into its own
        replay (Package 6 §4.3).

        Args:
            action: Required — ``dispatch`` or ``discard``.
            pending_event_ids: Required — non-empty list of pending event ids.
            reason: Required for ``discard`` — why these events may be
                dropped, at least 12 characters.  Recorded on every row.
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

        # Shape first, policy second: a malformed request is reported as
        # malformed rather than as a missing justification.
        reason = (_clean_str(args, "reason") or "").strip()
        if action == "discard" and len(reason) < MIN_PENDING_EVENT_REASON_LENGTH:
            return {"error": PENDING_EVENT_REASON_TOO_SHORT_ERROR}

        storage_methods = ["get_pending_events"]
        if action == "discard":
            storage_methods.append("resolve_pending_event")
        else:
            storage_methods.extend(
                (
                    "claim_pending_event_dispatch",
                    "renew_pending_event_dispatch_claim",
                    "finalize_pending_event_dispatch",
                    "record_pending_event_dispatch_failure",
                )
            )
        if not self._v2_storage_ready(*storage_methods):
            return self._v2_storage_unavailable()
        wanted = set(pending_event_ids)
        rows = await self.db.get_pending_events(pending_event_ids)
        found = {row["pending_event_id"]: row for row in rows if row["pending_event_id"] in wanted}
        from src.commands.principal import ExecutionPrincipal, current_principal

        principal = current_principal() or ExecutionPrincipal.service("playbook-pending-event")
        actor = principal.describe()
        dispatched: list[str] = []
        discarded: list[str] = []
        skipped: list[str] = []
        errors: list[str] = []
        for event_id in pending_event_ids:
            row = found.get(event_id)
            if row is None:
                skipped.append(event_id)
                continue
            if action == "discard":
                resolved = await self.db.resolve_pending_event(
                    event_id,
                    resolution="discarded",
                    resolved_by=actor,
                    resolution_reason=reason,
                    now=time.time(),
                )
                if not resolved:
                    skipped.append(event_id)
                    continue
                discarded.append(event_id)
                continue
            claimed, run_ids, error = await self._v2_replay_held_event(row, principal, actor)
            if not claimed:
                skipped.append(event_id)
                continue
            if error:
                errors.append(f"{event_id}: {error}")
                continue
            dispatched.extend(run_ids)
        return {
            "success": not errors,
            "action": action,
            "requested": len(pending_event_ids),
            "dispatched_run_ids": dispatched,
            "discarded_ids": discarded,
            "skipped": skipped,
            "errors": errors,
        }
