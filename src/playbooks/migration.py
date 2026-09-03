"""V1 → V2 playbook inventory and migration readiness — **no schema migration**.

Despite the name this module performs no database migration and is entirely
unrelated to :mod:`src.database.hierarchy_migration`, which is Alembic data
migration logic.  Nothing here writes anything: it reads the vault's authoring
Markdown, the compiled V1 store, the V2 activation rows and the operator
acknowledgement table, and reports what still stands between the fleet and a
V2 cutover.  No Alembic revision may import this module.

Package 6 of the Playbook V2 roadmap
(``docs/superpowers/plans/2026-09-01-playbook-v2-migration-artifacts.md``
§3.2 types, §3.3 disposition rules).  The one deliverable of this package is
*evidence*, and :func:`build_inventory` is where the evidence is gathered.

**Read-only is an invariant, not a style preference.**  ``build_inventory``
never compiles, activates, writes a file or emits an event, which is what lets
it run before the review UI exists.  ``tests/test_playbook_migration_inventory.py``
pins that with doubles whose every write raises.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import yaml

from src.playbooks.artifact_ref import SHA256_RE, ArtifactRef, ArtifactRefError
from src.playbooks.handler import PLAYBOOK_PATTERNS, derive_playbook_scope
from src.playbooks.routing import is_deprecated_default_assignment_entry

logger = logging.getLogger(__name__)

#: The three exact-match capability namespaces (Package 0).
_CAPABILITY_NAMESPACES: tuple[str, ...] = ("harness_tools", "aq_commands", "plugin_tools")


PlaybookDisposition = Literal["ready", "question_required", "invalid", "disabled"]


#: Closed set of operator-facing reason codes (child plan §3.2).  The cutover
#: report and the CLI both switch on these, so a code outside the set is a
#: programming error rather than a runtime condition.
REASON_CODES: frozenset[str] = frozenset(
    {
        "source_unreadable",
        "duplicate_playbook_id",
        "scope_conflict",
        "embedded_action_block",
        "compile_question",
        "schema_violation",
        "unknown_command",
        "unknown_event",
        "unknown_profile",
        "capability_not_declared",
        "binding_unassigned",
        "nested_loop_rejected",
        "stale_contract",
        "superseded_rule",
        "operator_disabled",
    }
)

#: Reason codes that no human decision can resolve — the source or the artifact
#: is wrong and must be fixed (§3.3 rule 1).
_FATAL_CODES: frozenset[str] = frozenset(
    {
        "source_unreadable",
        "duplicate_playbook_id",
        "schema_violation",
        "unknown_command",
        "unknown_event",
        "unknown_profile",
        "binding_unassigned",
        "nested_loop_rejected",
    }
)

#: ``ActivationHealth`` values that map onto a reason code.  ``ready`` and
#: ``needs_rebuild``/``unavailable`` are handled separately: the first produces
#: no reason and the others are transient operational states rather than
#: migration blockers.
_HEALTH_REASONS: Mapping[str, tuple[str, str]] = {
    "invalid_artifact": (
        "schema_violation",
        "the active artifact does not satisfy the strict V2 schema",
    ),
    "unknown_command": (
        "unknown_command",
        "the active artifact calls a command the contract registry does not define",
    ),
    "unknown_event": (
        "unknown_event",
        "the active artifact triggers on an event the registry does not define",
    ),
    "unknown_profile": (
        "unknown_profile",
        "the active artifact names an AI profile the vault does not define",
    ),
    "capability_not_declared": (
        "capability_not_declared",
        "the active artifact uses a capability its profile does not grant",
    ),
    "binding_unassigned": (
        "binding_unassigned",
        "the active artifact reads a binding that is not definitely assigned",
    ),
    "nested_loop_rejected": (
        "nested_loop_rejected",
        "the active artifact nests a for-each loop, which V2 rejects",
    ),
    "stale_contract": (
        "stale_contract",
        "the active artifact was compiled against a superseded command contract",
    ),
}


class MigrationReasonError(ValueError):
    """A reason code outside :data:`REASON_CODES` was constructed."""


@dataclass(frozen=True, slots=True)
class MigrationReason:
    """One operator-facing explanation for an entry's disposition.

    ``message`` is a single sentence written for a human deciding what to do
    next — never a stack trace and never a bare exception ``repr``.
    """

    code: str
    message: str
    source_line: int | None = None

    def __post_init__(self) -> None:
        if self.code not in REASON_CODES:
            raise MigrationReasonError(
                f"unknown migration reason code {self.code!r}; "
                f"the closed set is {sorted(REASON_CODES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "source_line": self.source_line,
        }


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Where an entry's authoring Markdown lives and what it currently hashes to."""

    vault_rel_path: str
    bundled_rel_path: str | None
    source_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "vault_rel_path": self.vault_rel_path,
            "bundled_rel_path": self.bundled_rel_path,
            "source_sha256": self.source_sha256,
        }


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    """One playbook's migration readiness."""

    playbook_id: str
    scope: str
    scope_identifier: str | None
    source: SourceRef
    v1_kind: str
    v1_version: int | None
    v1_enabled: bool
    disposition: PlaybookDisposition
    reasons: tuple[MigrationReason, ...]
    artifact: ArtifactRef | None
    activation_health: str | None
    has_embedded_action_block: bool
    acknowledged_by: str | None = None
    acknowledged_at: float | None = None
    pending_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "scope": self.scope,
            "scope_identifier": self.scope_identifier,
            "source": self.source.to_dict(),
            "v1_kind": self.v1_kind,
            "v1_version": self.v1_version,
            "v1_enabled": self.v1_enabled,
            "disposition": self.disposition,
            "reasons": [reason.to_dict() for reason in self.reasons],
            "artifact": self.artifact.as_dict() if self.artifact is not None else None,
            "activation_health": self.activation_health,
            "has_embedded_action_block": self.has_embedded_action_block,
            "acknowledged_by": self.acknowledged_by,
            "acknowledged_at": self.acknowledged_at,
            "pending_events": self.pending_events,
        }


@dataclass(frozen=True, slots=True)
class MigrationInventory:
    """The whole fleet's readiness at one instant."""

    generated_at: float
    contract_fingerprint: str
    entries: tuple[InventoryEntry, ...]

    def by_disposition(self, disposition: PlaybookDisposition) -> tuple[InventoryEntry, ...]:
        return tuple(e for e in self.entries if e.disposition == disposition)

    def blocking(self) -> tuple[InventoryEntry, ...]:
        """Entries that block cutover: everything not ``ready``, minus disabled.

        A ``disabled`` entry is out of the way either because its author wrote
        ``enabled: false`` or because an operator recorded an acknowledgement
        against the *current* source bytes.  Both are decisions someone made on
        purpose, so neither blocks.
        """
        return tuple(e for e in self.entries if e.disposition in ("question_required", "invalid"))

    def counts(self) -> dict[str, int]:
        counts = {"ready": 0, "question_required": 0, "invalid": 0, "disabled": 0}
        for entry in self.entries:
            counts[entry.disposition] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        """Stable key order — the API DTO and the CLI both render this."""
        return {
            "generated_at": self.generated_at,
            "contract_fingerprint": self.contract_fingerprint,
            "counts": self.counts(),
            "blocking": len(self.blocking()),
            "pending_events_total": sum(e.pending_events for e in self.entries),
            "entries": [entry.to_dict() for entry in self.entries],
        }


# ---------------------------------------------------------------------------
# Source enumeration
# ---------------------------------------------------------------------------


#: Vault-relative directories whose sources ship with the package, mapped to the
#: bundled tree they are copied from.  ``ensure_default_playbooks`` and
#: ``ensure_default_agent_type_playbooks`` are the copiers; recording the
#: provenance here is what lets an operator diff a customised vault copy against
#: what this build ships.
_BUNDLED_ROOTS: tuple[tuple[str, str], ...] = (
    ("system/playbooks", "src/prompts/default_playbooks"),
    ("agent-types/claude-opus/playbooks", "src/prompts/default_agent_type_playbooks/claude-opus"),
)


@dataclass(frozen=True, slots=True)
class _SourceScan:
    vault_rel_path: str
    scope: str
    scope_identifier: str | None
    raw: bytes
    playbook_id: str | None
    frontmatter: Mapping[str, Any]
    parse_error: str | None
    action_block_line: int | None

    @property
    def source_sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.raw).hexdigest()


def _sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bundled_rel_path(vault_rel_path: str) -> str | None:
    directory, _, filename = vault_rel_path.rpartition("/")
    for vault_dir, bundled_dir in _BUNDLED_ROOTS:
        if directory != vault_dir:
            continue
        absolute = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            *bundled_dir.split("/")[1:],
            filename,
        )
        if os.path.isfile(absolute):
            return f"{bundled_dir}/{filename}"
    return None


def find_embedded_action_block(text: str) -> int | None:
    """Return the 1-based line of the first embedded *action graph* fence.

    Classifies fences rather than counting them (child plan §1.1): a
    ```` ```json ```` block that holds a step-output example — as
    ``memory-consolidation.md`` does twice — is prose, not a graph.  Only a
    block that decodes to an object carrying a ``rules`` list of node-bearing
    entries is the V1 embedded action graph the rebuild has to remove.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() not in ("```json", "```JSON"):
            continue
        closing = next(
            (j for j in range(index + 1, len(lines)) if lines[j].strip() == "```"), None
        )
        if closing is None:
            continue
        try:
            payload = json.loads("\n".join(lines[index + 1 : closing]))
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        rules = payload.get("rules")
        if not isinstance(rules, list) or not rules:
            continue
        if any(isinstance(rule, Mapping) and "nodes" in rule for rule in rules):
            return index + 1
    return None


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str | None]:
    """Split YAML frontmatter, returning ``(metadata, error)``."""
    if not text.startswith("---"):
        return {}, "the file has no YAML frontmatter"
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, "the YAML frontmatter block is not terminated"
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        return {}, f"the YAML frontmatter does not parse ({exc.__class__.__name__})"
    if not isinstance(meta, Mapping):
        return {}, "the YAML frontmatter is not a mapping"
    return dict(meta), None


def _enumerate_sources(vault_root: str) -> list[_SourceScan]:
    """Walk the vault with the live discovery globs and classify each hit.

    Reuses ``PLAYBOOK_PATTERNS`` + ``derive_playbook_scope`` — the same pair
    ``PlaybookManager.reconcile_compilations`` uses — so the inventory can never
    disagree with the runtime about which files are playbooks.
    """
    import glob as globlib

    scans: list[_SourceScan] = []
    seen: set[str] = set()
    for pattern in PLAYBOOK_PATTERNS:
        for path in sorted(globlib.glob(os.path.join(vault_root, pattern))):
            rel = os.path.relpath(path, vault_root).replace(os.sep, "/")
            if rel in seen:
                continue
            seen.add(rel)
            scope, identifier = derive_playbook_scope(rel)
            try:
                with open(path, "rb") as handle:
                    raw = handle.read()
                text = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                scans.append(
                    _SourceScan(
                        vault_rel_path=rel,
                        scope=scope,
                        scope_identifier=identifier,
                        raw=b"",
                        playbook_id=None,
                        frontmatter={},
                        parse_error=f"the source cannot be read ({exc.__class__.__name__})",
                        action_block_line=None,
                    )
                )
                continue
            meta, error = _parse_frontmatter(text)
            playbook_id = meta.get("id") if isinstance(meta.get("id"), str) else None
            if error is None and not playbook_id:
                error = "the frontmatter declares no 'id'"
            scans.append(
                _SourceScan(
                    vault_rel_path=rel,
                    scope=scope,
                    scope_identifier=identifier,
                    raw=raw,
                    playbook_id=playbook_id,
                    frontmatter=meta,
                    parse_error=error,
                    action_block_line=find_embedded_action_block(text),
                )
            )
    return scans


def _fallback_id(vault_rel_path: str) -> str:
    """Identify an unreadable source by its filename stem.

    A source with no usable ``id`` still has to appear in the inventory —
    silently dropping it is exactly the failure mode the package exists to
    prevent — so it is keyed by its filename and reported ``invalid``.
    """
    return os.path.splitext(vault_rel_path.rsplit("/", 1)[-1])[0]


# ---------------------------------------------------------------------------
# Scope comparison
# ---------------------------------------------------------------------------


def _normalise_frontmatter_scope(value: Any) -> tuple[str, str | None] | None:
    """Map an authored ``scope:`` string onto ``derive_playbook_scope``'s pair."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    head, _, identifier = raw.partition(":")
    head = head.strip().replace("-", "_")
    identifier = identifier.strip() or None
    if head in ("system", "supervisor"):
        return head, None
    if head in ("agent_type", "project"):
        return head, identifier
    return None


# ---------------------------------------------------------------------------
# Repository adapters — every one optional and failure-tolerant
# ---------------------------------------------------------------------------


async def _safe_call(obj: Any, name: str, *, default: Any, **kwargs: Any) -> Any:
    """Call an optional repository method, degrading to ``default``.

    The inventory is a reporting surface: a repository that is absent, that
    raises, or that a test replaced with a narrower double must degrade the
    report, never abort it.
    """
    if obj is None:
        return default
    method = getattr(obj, name, None)
    if method is None:
        return default
    try:
        result = method(**kwargs)
        if hasattr(result, "__await__"):
            result = await result
    except Exception:  # pragma: no cover - defensive; logged for the operator
        logger.warning("migration inventory: %s.%s failed", type(obj).__name__, name, exc_info=True)
        return default
    return default if result is None else result


def _activation_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("playbook_id") or ""),
        str(row.get("scope") or "system"),
        str(row.get("scope_identifier") or ""),
    )


def _artifact_ref(row: Mapping[str, Any]) -> ArtifactRef | None:
    """Project an activation row onto :class:`ArtifactRef`, or ``None``.

    A row whose hashes are malformed is not an error the inventory reports as a
    crash: the activation health already carries that condition, and a missing
    ``artifact`` field is the honest projection.
    """
    try:
        return ArtifactRef(
            playbook_id=str(row.get("playbook_id") or ""),
            artifact_sha256=str(row.get("artifact_sha256") or ""),
            schema_generation=int(row.get("schema_generation") or 2),
            contract_fingerprint=str(row.get("contract_fingerprint") or ""),
            source_digest=str(row.get("source_digest") or ""),
            compiler_build=str(row.get("compiler_build") or "unknown"),
            compiled_at=row.get("compiled_at"),
            version=int(row.get("version") or 0),
        )
    except (ArtifactRefError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def _classify(
    *,
    scans: Sequence[_SourceScan],
    compiled: Mapping[str, Any],
    activations: Mapping[tuple[str, str, str], Mapping[str, Any]],
    acks: Mapping[tuple[str, str, str], Mapping[str, Any]],
    pending: Mapping[str, int],
    contract_fingerprint: str,
) -> InventoryEntry:
    """Build one entry from every source claiming the same playbook id.

    ``scans`` holds at least one element; more than one means two files claim
    the same ``id``, which is fatal and reported once rather than as a pair of
    half-entries.
    """
    primary = scans[0]
    playbook_id = primary.playbook_id or _fallback_id(primary.vault_rel_path)
    reasons: list[MigrationReason] = []

    source = SourceRef(
        vault_rel_path=primary.vault_rel_path,
        bundled_rel_path=_bundled_rel_path(primary.vault_rel_path),
        source_sha256=primary.source_sha256,
    )

    if len(scans) > 1:
        paths = ", ".join(scan.vault_rel_path for scan in scans)
        reasons.append(
            MigrationReason(
                code="duplicate_playbook_id",
                message=f"{len(scans)} sources declare id '{playbook_id}': {paths}",
            )
        )
    if primary.parse_error:
        reasons.append(
            MigrationReason(code="source_unreadable", message=primary.parse_error, source_line=1)
        )

    # V1 compiled artifact, when one was ever produced.
    v1 = compiled.get(playbook_id)
    v1_kind = str(getattr(v1, "kind", "") or "") if v1 is not None else ""
    v1_version = int(getattr(v1, "version", 0) or 0) if v1 is not None else None
    frontmatter_enabled = primary.frontmatter.get("enabled")
    v1_enabled = bool(getattr(v1, "enabled", True)) if v1 is not None else frontmatter_enabled is not False

    superseded = _superseded_entries(v1)
    if superseded:
        reasons.append(
            MigrationReason(
                code="superseded_rule",
                message=(
                    "the cached V1 artifact still carries rule "
                    f"{', '.join(superseded)}, superseded by the assignment router; "
                    "it is not a missing rule"
                ),
            )
        )

    # Scope authority: the path wins, the frontmatter claim is discarded.
    authored_scope = _normalise_frontmatter_scope(primary.frontmatter.get("scope"))
    if authored_scope is not None and authored_scope != (primary.scope, primary.scope_identifier):
        reasons.append(
            MigrationReason(
                code="scope_conflict",
                message=(
                    f"the frontmatter claims scope '{primary.frontmatter.get('scope')}' but "
                    f"'{primary.vault_rel_path}' installs at scope "
                    f"{primary.scope}:{primary.scope_identifier or '-'}; the path wins and the "
                    "frontmatter claim is discarded"
                ),
            )
        )

    if primary.action_block_line is not None:
        reasons.append(
            MigrationReason(
                code="embedded_action_block",
                message=(
                    "the source still embeds a V1 action graph; V2 authoring sources are prose "
                    "and the graph must be rebuilt and reviewed"
                ),
                source_line=primary.action_block_line,
            )
        )

    activation = activations.get(
        (playbook_id, primary.scope, primary.scope_identifier or "")
    )
    artifact = _artifact_ref(activation) if activation is not None else None
    health = str(activation.get("health")) if activation is not None else None

    if activation is not None:
        mapped = _HEALTH_REASONS.get(health or "")
        if mapped is not None:
            reasons.append(MigrationReason(code=mapped[0], message=mapped[1]))
        elif artifact is not None and artifact.contract_fingerprint != contract_fingerprint:
            reasons.append(
                MigrationReason(
                    code="stale_contract",
                    message=(
                        "the active artifact was compiled against contract fingerprint "
                        f"{artifact.contract_fingerprint[:19]}…, but this build serves "
                        f"{contract_fingerprint[:19]}…; recompile and re-review"
                    ),
                )
            )
        elif artifact is not None and artifact.source_digest != primary.source_sha256:
            reasons.append(
                MigrationReason(
                    code="compile_question",
                    message=(
                        "the authoring Markdown changed after the active artifact was "
                        "reviewed; recompile and re-review before cutover"
                    ),
                )
            )
    elif not primary.parse_error and len(scans) == 1:
        reasons.append(
            MigrationReason(
                code="compile_question",
                message="no reviewed V2 artifact is active for this playbook yet",
            )
        )

    ack = acks.get((playbook_id, primary.scope, primary.scope_identifier or ""))
    ack_matches = ack is not None and str(ack.get("source_sha256")) == primary.source_sha256

    disposition, acknowledged_by, acknowledged_at = _disposition(
        reasons=reasons,
        frontmatter_disabled=frontmatter_enabled is False,
        ack=ack if ack_matches else None,
    )
    if disposition == "disabled":
        reasons = [r for r in reasons if r.code == "operator_disabled"] + [
            MigrationReason(
                code="operator_disabled",
                message=(
                    "the source declares 'enabled: false'"
                    if frontmatter_enabled is False
                    else f"an operator acknowledged this playbook: {ack.get('reason')}"
                ),
            )
        ]
        reasons = reasons[-1:]

    return InventoryEntry(
        playbook_id=playbook_id,
        scope=primary.scope,
        scope_identifier=primary.scope_identifier,
        source=source,
        v1_kind=v1_kind,
        v1_version=v1_version,
        v1_enabled=v1_enabled,
        disposition=disposition,
        reasons=tuple(reasons),
        artifact=artifact,
        activation_health=health,
        has_embedded_action_block=primary.action_block_line is not None,
        acknowledged_by=acknowledged_by,
        acknowledged_at=acknowledged_at,
        pending_events=int(pending.get(playbook_id, 0)),
    )


def _superseded_entries(compiled: Any) -> list[str]:
    if compiled is None:
        return []
    nodes = getattr(compiled, "nodes", None)
    if not isinstance(nodes, Mapping):
        return []
    return sorted(
        entry for entry in nodes if is_deprecated_default_assignment_entry(compiled, entry)
    )


def _disposition(
    *,
    reasons: Sequence[MigrationReason],
    frontmatter_disabled: bool,
    ack: Mapping[str, Any] | None,
) -> tuple[PlaybookDisposition, str | None, float | None]:
    """Apply §3.3's ordered rules; first match wins."""
    codes = {reason.code for reason in reasons}
    if codes & _FATAL_CODES:
        return "invalid", None, None
    if frontmatter_disabled:
        return "disabled", None, None
    if ack is not None:
        return (
            "disabled",
            str(ack.get("acknowledged_by")) if ack.get("acknowledged_by") else None,
            float(ack["acknowledged_at"]) if ack.get("acknowledged_at") is not None else None,
        )
    if codes - {"superseded_rule"}:
        return "question_required", None, None
    return "ready", None, None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def build_inventory(
    *,
    vault_root: str,
    store: Any = None,
    contract_registry: Any = None,
    activation_repo: Any = None,
    ack_repo: Any = None,
    pending_repo: Any = None,
    db: Any = None,
    now: float | None = None,
) -> MigrationInventory:
    """Inventory every playbook this vault installs and classify its readiness.

    Parameters
    ----------
    vault_root:
        Absolute path to the vault (the directory holding ``system/``).
    store:
        A :class:`~src.playbooks.store.CompiledPlaybookStore`.  Only
        ``list_all()`` is called; ``save``/``delete`` never are.
    contract_registry:
        Package 1's ``ContractRegistry`` — read for its registry-wide
        fingerprint, which every entry's artifact is compared against.
    activation_repo, ack_repo, pending_repo:
        Optional read-only repositories.  ``None`` (or a repository that
        raises) degrades the corresponding fields rather than the whole report.
    db:
        Accepted and unused.  Present so callers can hand the inventory the
        same database handle every other command takes, and so the read-only
        test can pass a double that raises on every statement.
    """
    del db  # read-only by contract; see the module docstring

    scans = _enumerate_sources(vault_root)

    by_id: dict[str, list[_SourceScan]] = {}
    for scan in scans:
        key = scan.playbook_id or _fallback_id(scan.vault_rel_path)
        by_id.setdefault(key, []).append(scan)

    compiled: dict[str, Any] = {}
    if store is not None:
        try:
            for _scope, _identifier, playbook in store.list_all():
                compiled[str(getattr(playbook, "id", ""))] = playbook
        except Exception:  # pragma: no cover - defensive
            logger.warning("migration inventory: compiled store scan failed", exc_info=True)

    activation_rows = await _safe_call(
        activation_repo, "list_playbook_activations", default=[]
    )
    activations = {
        _activation_key(row): row for row in activation_rows if isinstance(row, Mapping)
    }

    ack_rows = await _safe_call(ack_repo, "list_acks", default=[])
    acks = {
        (
            str(row.get("playbook_id") or ""),
            str(row.get("scope") or "system"),
            str(row.get("scope_identifier") or ""),
        ): row
        for row in ack_rows
        if isinstance(row, Mapping)
    }

    pending_rows = await _safe_call(pending_repo, "list_pending_events", default=[])
    pending: dict[str, int] = {}
    for row in pending_rows:
        if isinstance(row, Mapping):
            pid = str(row.get("playbook_id") or "")
            pending[pid] = pending.get(pid, 0) + 1

    fingerprint = "sha256:" + "0" * 64
    if contract_registry is not None:
        try:
            fingerprint = str(contract_registry.registry_fingerprint())
        except Exception:  # pragma: no cover - defensive
            logger.warning("migration inventory: contract fingerprint unavailable", exc_info=True)

    entries = [
        _classify(
            scans=scan_group,
            compiled=compiled,
            activations=activations,
            acks=acks,
            pending=pending,
            contract_fingerprint=fingerprint,
        )
        for scan_group in by_id.values()
    ]

    # A compiled artifact whose source has vanished is never silently dropped:
    # the fleet may still execute it, so it is reported `invalid`.
    for playbook_id, playbook in compiled.items():
        if playbook_id in by_id or not playbook_id:
            continue
        entries.append(_orphan_entry(playbook_id, playbook, pending))

    entries.sort(key=lambda e: (e.scope, e.scope_identifier or "", e.playbook_id))
    return MigrationInventory(
        generated_at=time.time() if now is None else now,
        contract_fingerprint=fingerprint,
        entries=tuple(entries),
    )


def _orphan_entry(
    playbook_id: str, playbook: Any, pending: Mapping[str, int]
) -> InventoryEntry:
    scope = str(getattr(playbook, "scope", "system") or "system")
    scope = getattr(scope, "value", scope)
    return InventoryEntry(
        playbook_id=playbook_id,
        scope=scope if scope in ("system", "supervisor", "agent_type", "project") else "system",
        scope_identifier=None,
        source=SourceRef(
            vault_rel_path="",
            bundled_rel_path=None,
            source_sha256=_sha256_text(""),
        ),
        v1_kind=str(getattr(playbook, "kind", "") or ""),
        v1_version=int(getattr(playbook, "version", 0) or 0),
        v1_enabled=bool(getattr(playbook, "enabled", True)),
        disposition="invalid",
        reasons=(
            MigrationReason(
                code="source_unreadable",
                message=(
                    f"a compiled V1 artifact for '{playbook_id}' exists but its authoring "
                    "Markdown is missing from the vault"
                ),
            ),
        ),
        artifact=None,
        activation_health=None,
        has_embedded_action_block=False,
        pending_events=int(pending.get(playbook_id, 0)),
    )



# ---------------------------------------------------------------------------
# §5.3 T-9 — capability audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapabilityFinding:
    """One capability an artifact needs that a review record does not list."""

    namespace: str          # harness_tools | aq_commands | plugin_tools
    name: str               # the command or tool
    step_id: str            # the step that needs it
    rule_id: str | None


def required_capabilities(definition: Any) -> dict[str, set[str]]:
    """What one artifact actually needs, split by capability namespace.

    A ``command`` step needs its command in ``aq_commands``; an ``llm`` step
    needs each name in its ``tool_use`` allowance, classified the same way
    :func:`src.profiles.capabilities.classify_capability` classifies it, so a
    plugin tool is not audited as an AQ command.
    """
    from src.profiles.capabilities import classify_capability

    required: dict[str, set[str]] = {namespace: set() for namespace in _CAPABILITY_NAMESPACES}
    for step in getattr(definition, "steps", {}).values():
        command = getattr(step, "command", None)
        if isinstance(command, str) and command:
            required["aq_commands"].add(command)
        for name in getattr(step, "tool_use", None) or ():
            if isinstance(name, str) and name:
                required[classify_capability(name)].add(name)
    return required


def audit_capabilities(definition: Any, policy: Any) -> tuple[CapabilityFinding, ...]:
    """Every capability the artifact needs that *policy* does not grant.

    *policy* is either a :class:`~src.profiles.capabilities.CapabilityPolicy`
    or a plain mapping of namespace to names — the shape a reviewed fixture's
    ``review.md`` records for the capabilities a human approved.

    **One direction only** (child plan §4.1).  A finding means the artifact
    exceeds what was approved.  A capability listed in the review but unused by
    the artifact is not a finding, because this function must never be readable
    as a way to *grant* something: a repository write is not a privilege grant.
    """
    granted: dict[str, frozenset[str]] = {}
    for namespace in _CAPABILITY_NAMESPACES:
        if isinstance(policy, Mapping):
            names = policy.get(namespace) or ()
        else:
            names = getattr(policy, namespace, None) or ()
        granted[namespace] = frozenset(str(name) for name in names)

    findings: list[CapabilityFinding] = []
    for step_id, step in sorted(getattr(definition, "steps", {}).items()):
        needed: list[tuple[str, str]] = []
        command = getattr(step, "command", None)
        if isinstance(command, str) and command:
            needed.append(("aq_commands", command))
        for name in getattr(step, "tool_use", None) or ():
            if isinstance(name, str) and name:
                from src.profiles.capabilities import classify_capability

                needed.append((classify_capability(name), name))
        for namespace, name in needed:
            if name in granted[namespace]:
                continue
            findings.append(
                CapabilityFinding(
                    namespace=namespace,
                    name=name,
                    step_id=step_id,
                    rule_id=getattr(step, "rule", None),
                )
            )
    return tuple(findings)


__all__ = [
    "REASON_CODES",
    "SHA256_RE",
    "CapabilityFinding",
    "InventoryEntry",
    "MigrationInventory",
    "MigrationReason",
    "MigrationReasonError",
    "PlaybookDisposition",
    "SourceRef",
    "audit_capabilities",
    "build_inventory",
    "find_embedded_action_block",
    "required_capabilities",
]
