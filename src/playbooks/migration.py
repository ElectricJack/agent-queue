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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from src.playbooks.activation import ActivationHealth
from src.playbooks.artifact_ref import SHA256_RE, ArtifactRef, ArtifactRefError
from src.playbooks.definition import source_digest
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

#: Every non-ready ``ActivationHealth`` maps onto an operator-facing reason.
#: Keeping the enum members here makes drift between the activation subsystem
#: and the migration inventory a type-visible change instead of a silent
#: fall-through to ``ready``.
_HEALTH_REASONS: Mapping[ActivationHealth, tuple[str, str]] = {
    ActivationHealth.INVALID: (
        "schema_violation",
        "the active artifact does not satisfy the strict V2 schema",
    ),
    ActivationHealth.QUESTION_REQUIRED: (
        "compile_question",
        "the active artifact has unresolved compiler questions",
    ),
    ActivationHealth.DISABLED: (
        "compile_question",
        "the V2 activation is disabled without 'enabled: false' in source or a "
        "source-matched operator acknowledgement",
    ),
    ActivationHealth.STALE_CONTRACT: (
        "stale_contract",
        "the active artifact was compiled against a superseded command contract",
    ),
    ActivationHealth.UNAVAILABLE: (
        "compile_question",
        "the active artifact is unavailable; restore or recompile it before cutover",
    ),
}

# Older inventory inputs used validator diagnostics as the health string.  They
# are not legal ``ActivationHealth`` values, but retaining this read adapter
# preserves the more specific migration reason when an old snapshot or a
# pre-enum repository double is inspected.  Live rows always take the enum path
# above, and any other unknown string fails closed below.
_LEGACY_HEALTH_REASONS: Mapping[str, tuple[str, str]] = {
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
# §5.4 — deterministic V1/V2 shadow comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    """One command a shadow arm would have invoked, after normalisation."""

    order: int
    command: str
    args_canonical: str


@dataclass(frozen=True, slots=True)
class AuthzDecision:
    """The dispatch-boundary decision for one command."""

    command: str
    principal_kind: str
    allowed: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    """The comparable, side-effect-free record of one V1 or V2 event arm."""

    arm: Literal["v1", "v2"]
    event_id: str
    event_type: str
    rules_selected: tuple[str, ...]
    node_path: tuple[str, ...]
    commands: tuple[CommandInvocation, ...]
    routing_outputs: Mapping[str, Any]
    terminal: str
    authorization: tuple[AuthzDecision, ...]


DifferenceClass = Literal["identical", "expected_v2_semantics", "unexplained"]


@dataclass(frozen=True, slots=True)
class ParityFinding:
    """One visible difference between the two arms of a shadow observation."""

    field: Literal[
        "rules_selected", "node_path", "commands", "routing_outputs", "terminal", "authorization"
    ]
    v1: Any
    v2: Any
    classification: DifferenceClass
    rationale_id: str | None = None

    def __post_init__(self) -> None:
        if self.classification == "expected_v2_semantics":
            if self.field == "authorization":
                raise ValueError("authorization differences are never waivable")
            if self.rationale_id not in EXPECTED_DIFFERENCES:
                raise ValueError(f"unknown expected-difference rationale {self.rationale_id!r}")
        elif self.rationale_id is not None:
            raise ValueError("only an expected difference may carry a rationale id")


#: The only intentional semantic deviations a parity report may name.  Keeping
#: this closed and in the executable comparison module prevents a report from
#: becoming a free-form waiver document.
EXPECTED_DIFFERENCES: Mapping[str, str] = {
    "run-per-rule": (
        "V2 has one run per matching rule while V1 shared one run per event; run identity "
        "is deliberately excluded from the observation."
    ),
    "rule-failure-isolation": (
        "A V2 rule failure does not abort sibling rules, whereas V1 stopped the shared event run."
    ),
    "loop-frame-shape": (
        "V2 stores loop state in a typed frame rather than V1's transient outputs dictionary; "
        "per-iteration commands remain comparable."
    ),
    "unassigned-ref-rejected": (
        "V2 rejects an unassigned binding at compile time where V1 substituted an empty or null value."
    ),
    "terminal-vocabulary": (
        "V2 distinguishes timed_out and cancelled from V1's failed terminal vocabulary."
    ),
    "null-template-part-rendered": (
        "A template part that resolves to null renders as the literal 'null' in V2 and as an "
        "empty string in V1. V1's ``_substitute`` blanked the hole silently "
        "(src/playbooks/pipeline_runner.py:48-63); V2's ``_render`` states it, because "
        "'a template can never silently render a hole' (src/playbooks/expressions.py:429-431). "
        "It is admissible only when the two argument sets are otherwise identical key for key "
        "and value for value, so it can never absorb a changed, added or dropped argument."
    ),
}


_PARITY_FIELDS: tuple[str, ...] = (
    "rules_selected",
    "node_path",
    "commands",
    "routing_outputs",
    "terminal",
    "authorization",
)


def _terminal_difference_is_expected(v1: str, v2: str) -> bool:
    """Only V2's new non-completed terminals are a vocabulary difference."""
    return v1 != "completed" and v2 in {"timed_out", "cancelled"}


def _null_rendering_only(left: str, right: str) -> bool:
    """``right`` is ``left`` with one or more blanked holes rendered as ``null``.

    Deliberately structural rather than textual: both sides are parsed, the key
    sets must match exactly, and only ``(str, str)`` values may differ.  A
    changed, added or dropped argument therefore cannot reach this rule.
    """
    try:
        a = json.loads(left)
        b = json.loads(right)
    except ValueError:
        return False
    if not isinstance(a, dict) or not isinstance(b, dict) or a.keys() != b.keys():
        return False
    changed = False
    for key, value in a.items():
        other = b[key]
        if value == other:
            continue
        if not isinstance(value, str) or not isinstance(other, str):
            return False
        if other.replace("null", "") != value:
            return False
        changed = True
    return changed


def _command_difference_is_expected(
    v1: Sequence[CommandInvocation], v2: Sequence[CommandInvocation]
) -> bool:
    """The one command-level rationale: a null template part, and nothing else.

    The comparison stays a projection everywhere else — a different command, a
    different order, an extra or missing invocation, or any argument change
    that is not exactly V1's blanked hole is ``unexplained``.
    """
    if len(v1) != len(v2):
        return False
    saw_difference = False
    for left, right in zip(v1, v2, strict=True):
        if (left.order, left.command) != (right.order, right.command):
            return False
        if left.args_canonical == right.args_canonical:
            continue
        if not _null_rendering_only(left.args_canonical, right.args_canonical):
            return False
        saw_difference = True
    return saw_difference


def compare(v1: ShadowObservation, v2: ShadowObservation) -> tuple[ParityFinding, ...]:
    """Compare the closed observation surface and leave unknown differences loud.

    This function intentionally has no heuristic to turn a changed command,
    route, or authorization result into an expected difference.  New semantic
    intent needs an explicit reviewed rationale and an observation exercising
    it; otherwise cutover remains blocked.
    """
    if v1.arm != "v1" or v2.arm != "v2":
        raise ValueError("compare requires a v1 observation followed by a v2 observation")
    if (v1.event_id, v1.event_type) != (v2.event_id, v2.event_type):
        raise ValueError("compare requires observations for the same event id and type")

    findings: list[ParityFinding] = []
    for field in _PARITY_FIELDS:
        left = getattr(v1, field)
        right = getattr(v2, field)
        if left == right:
            continue
        if field == "terminal" and _terminal_difference_is_expected(left, right):
            findings.append(
                ParityFinding(
                    field="terminal",
                    v1=left,
                    v2=right,
                    classification="expected_v2_semantics",
                    rationale_id="terminal-vocabulary",
                )
            )
        elif field == "commands" and _command_difference_is_expected(left, right):
            findings.append(
                ParityFinding(
                    field="commands",
                    v1=left,
                    v2=right,
                    classification="expected_v2_semantics",
                    rationale_id="null-template-part-rendered",
                )
            )
        else:
            findings.append(
                ParityFinding(
                    field=field,  # type: ignore[arg-type]
                    v1=left,
                    v2=right,
                    classification="unexplained",
                )
            )
    return tuple(findings)


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

    @property
    def source_digest(self) -> str:
        return source_digest(self.raw.decode("utf-8"))


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


async def _activation_rows(activation_repo: Any) -> list[Any]:
    """Activation rows carrying their artifact's identity columns.

    An activation row on its own names an artifact hash and nothing else, so
    the drift checks below — is the artifact still compiled against this
    contract fingerprint, and against the source bytes on disk? — have no
    evidence to compare.  The joined read supplies it; a repository that
    predates the join (or a narrower test double) still degrades to the
    unjoined rows rather than to no report at all.
    """
    rows = await _safe_call(
        activation_repo, "list_playbook_activations_with_artifacts", default=None
    )
    if rows is None:
        rows = await _safe_call(activation_repo, "list_playbook_activations", default=[])
    return list(rows)


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


def _current_artifact_contract_fingerprint(
    artifact: ArtifactRef, artifact_store: Any, contract_registry: Any
) -> str:
    """Fingerprint the current contracts over the artifact's command set."""
    definition = artifact_store.load(artifact.artifact_sha256)
    current_commands: dict[str, str] = {}
    for name in definition.compiled_against.commands:
        try:
            current_commands[name] = str(contract_registry.fingerprint(name))
        except Exception:  # a removed command must produce a different aggregate
            current_commands[name] = ""
    compiled_against = definition.compiled_against.model_copy(
        update={"commands": current_commands}
    )
    return definition.model_copy(
        update={"compiled_against": compiled_against}
    ).contract_fingerprint()


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
    contract_registry: Any,
    artifact_store: Any,
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
    raw_health = activation.get("health") if activation is not None else None
    try:
        activation_health = ActivationHealth(raw_health) if raw_health is not None else None
    except (TypeError, ValueError):
        activation_health = None
    if activation_health is not None:
        health = activation_health.value
    elif activation is not None:
        health = str(raw_health)
    else:
        health = None

    if activation is not None:
        if artifact is None:
            reasons.append(
                MigrationReason(
                    code="schema_violation",
                    message="the activation does not reference a valid joined V2 artifact",
                )
            )
        elif legacy_reason := _LEGACY_HEALTH_REASONS.get(str(raw_health)):
            reasons.append(MigrationReason(code=legacy_reason[0], message=legacy_reason[1]))
        elif activation_health is None:
            reasons.append(
                MigrationReason(
                    code="schema_violation",
                    message=f"the activation reports unknown health state {raw_health!r}",
                )
            )
        elif (mapped := _HEALTH_REASONS.get(activation_health)) is not None:
            reasons.append(MigrationReason(code=mapped[0], message=mapped[1]))
        else:
            current_fingerprint = None
            fingerprint_unavailable = False
            if artifact_store is not None and contract_registry is not None:
                try:
                    current_fingerprint = _current_artifact_contract_fingerprint(
                        artifact, artifact_store, contract_registry
                    )
                except Exception:  # inventory reports unread evidence instead of aborting
                    logger.warning(
                        "migration inventory: active artifact %s unavailable",
                        artifact.artifact_sha256,
                        exc_info=True,
                    )
                    fingerprint_unavailable = True
            if fingerprint_unavailable:
                reasons.append(
                    MigrationReason(
                        code="compile_question",
                        message=(
                            "the active artifact cannot be read to compare command contracts; "
                            "restore or recompile it before cutover"
                        ),
                    )
                )
            elif (
                current_fingerprint is not None
                and artifact.contract_fingerprint != current_fingerprint
            ):
                reasons.append(
                    MigrationReason(
                        code="stale_contract",
                        message=(
                            "the active artifact was compiled against contract fingerprint "
                            f"{artifact.contract_fingerprint[:19]}…, but this build serves "
                            f"{current_fingerprint[:19]}… for the same commands; "
                            "recompile and re-review"
                        ),
                    )
                )
            elif artifact.source_digest != primary.source_digest:
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
        if frontmatter_enabled is False:
            disabled_reason = MigrationReason(
                code="operator_disabled",
                message="the source declares 'enabled: false'",
            )
        elif ack_matches:
            disabled_reason = MigrationReason(
                code="operator_disabled",
                message=f"an operator acknowledged this playbook: {ack.get('reason')}",
            )
        else:
            disabled_reason = next(r for r in reasons if r.code == "operator_disabled")
        reasons = [disabled_reason]

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
    # Runtime activation health is not operator evidence.  Only authoring
    # frontmatter or a source-scoped acknowledgement can satisfy this rule.
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
    artifact_store: Any = None,
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
    artifact_store:
        A :class:`~src.playbooks.artifact_store.ArtifactStore`, used read-only
        to recover each active artifact's command set.
    contract_registry:
        Package 1's ``ContractRegistry`` — read for its registry-wide report
        fingerprint and the current fingerprints of each artifact's commands.
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

    activation_rows = await _activation_rows(activation_repo)
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
            contract_registry=contract_registry,
            artifact_store=artifact_store,
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


# ---------------------------------------------------------------------------
# §5.5 — cutover evidence report
# ---------------------------------------------------------------------------


def _playbook_ids(rows: Sequence[Mapping[str, Any]]) -> str:
    """A short, stable ``a, b, c`` list for an operator-facing blocking reason."""
    return ", ".join(sorted({str(row.get("playbook_id") or "?") for row in rows}))


def build_cutover_report(
    *,
    contract_fingerprint: str,
    artifacts: Sequence[Mapping[str, Any]],
    unresolved: Sequence[Mapping[str, Any]],
    acknowledged_disabled: Sequence[Mapping[str, Any]],
    pending_events: Sequence[Mapping[str, Any]],
    active_v1_runs: Sequence[Mapping[str, Any]],
    parity: Mapping[str, Any],
    evidence_errors: Sequence[Mapping[str, Any]] = (),
    now: float | None = None,
) -> dict[str, Any]:
    """Render the complete, serialisable Package 6 cutover evidence.

    The report only assembles evidence collected by the caller; it never
    compiles, activates, replays, or otherwise alters fleet state.

    *evidence_errors* are the reads the caller could **not** perform, one
    ``{"source": ..., "error": ...}`` mapping each.  They exist because an
    unread evidence source and an empty one are not the same fact: a failed
    ``list_pending_events`` call rendered as zero pending events would let this
    report certify a fleet it never looked at.  Every entry is therefore a
    blocking reason, and the sections it fed are marked ``unavailable``.
    """
    generated_at = time.time() if now is None else now
    unread = [
        {
            "source": str(row.get("source") or "unknown"),
            "error": str(row.get("error") or "unavailable"),
        }
        for row in evidence_errors
    ]
    unread_sources = {row["source"] for row in unread}
    rendered_artifacts = [
        {
            "playbook_id": row.get("playbook_id"),
            "scope": row.get("scope", "system"),
            "scope_identifier": row.get("scope_identifier"),
            "artifact_sha256": row.get("artifact_sha256"),
            "source_sha256": row.get("source_sha256"),
            "activation_health": row.get("activation_health", row.get("health")),
            "reviewed_by": row.get("reviewed_by"),
            "reviewed_at": row.get("reviewed_at"),
            "v1_available": bool(row.get("v1_available", False)),
        }
        for row in artifacts
    ]
    by_playbook: dict[str, int] = {}
    received: list[float] = []
    for event in pending_events:
        playbook_id = str(event.get("playbook_id") or "unknown")
        by_playbook[playbook_id] = by_playbook.get(playbook_id, 0) + 1
        timestamp = event.get("received_at")
        if isinstance(timestamp, (int, float)):
            received.append(float(timestamp))

    runs: list[dict[str, Any]] = []
    running = paused = 0
    run_started: list[float] = []
    for row in active_v1_runs:
        status = str(row.get("status") or "")
        if status == "running":
            running += 1
        elif status == "paused":
            paused += 1
        else:
            continue
        runs.append(dict(row))
        timestamp = row.get("started_at")
        if isinstance(timestamp, (int, float)):
            run_started.append(float(timestamp))

    unexplained = int(parity.get("unexplained") or 0)
    # §3.7: an activation is rollback-ready only when a human approved the exact
    # bytes that are live *and* the V1 artifact it would roll back to still
    # exists.  Evidence that is merely absent counts as absent, never as fine —
    # a report assembled from rows that could not be joined would otherwise
    # declare a fleet cutover-eligible on the strength of four nulls.
    incomplete = [
        row
        for row in rendered_artifacts
        if not row.get("artifact_sha256") or not row.get("source_sha256")
    ]
    unreviewed = [
        row
        for row in rendered_artifacts
        if not row.get("reviewed_by") or not row.get("reviewed_at")
    ]
    # The claim is also withheld when the activation rows or V1 store could not
    # be read: absence of evidence from an unavailable source is not evidence of
    # rollback readiness.
    rollback_ready = (
        bool(rendered_artifacts)
        and not unreviewed
        and all(row.get("v1_available", False) for row in artifacts)
        and not (unread_sources & {"activations", "v1_store"})
    )
    blocking_reasons: list[str] = []
    for row in unread:
        blocking_reasons.append(
            f"evidence source {row['source']!r} could not be read ({row['error']}); "
            "cutover cannot be certified against evidence that was never collected"
        )
    if unresolved:
        blocking_reasons.append(f"{len(unresolved)} unresolved migration inventory entries")
    unhealthy = [row for row in rendered_artifacts if row.get("activation_health") != "ready"]
    if unhealthy:
        blocking_reasons.append(f"{len(unhealthy)} enabled activations are not ready")
    if incomplete:
        blocking_reasons.append(
            f"{len(incomplete)} enabled activations have incomplete artifact evidence "
            f"({_playbook_ids(incomplete)})"
        )
    if unreviewed:
        blocking_reasons.append(
            f"{len(unreviewed)} enabled activations have no recorded review of the live "
            f"artifact ({_playbook_ids(unreviewed)})"
        )
    if pending_events:
        blocking_reasons.append(f"{len(pending_events)} pending events require an operator decision")
    if runs:
        blocking_reasons.append(f"{len(runs)} active V1 runs must drain before cutover")
    if unexplained:
        blocking_reasons.append(f"{unexplained} unexplained shadow-parity findings")
    if parity.get("recorded") is False:
        blocking_reasons.append("no committed shadow-parity report is available")
    if not rollback_ready:
        blocking_reasons.append("rollback artifacts are incomplete")

    return {
        "success": True,
        "generated_at": generated_at,
        "contract_fingerprint": contract_fingerprint,
        "artifacts": rendered_artifacts,
        "unresolved": [dict(row) for row in unresolved],
        "acknowledged_disabled": [dict(row) for row in acknowledged_disabled],
        "pending_events": {
            "total": len(pending_events),
            "oldest_age_seconds": max(0.0, generated_at - min(received)) if received else None,
            "by_playbook": dict(sorted(by_playbook.items())),
            "unavailable": "pending_events" in unread_sources,
        },
        "active_v1_runs": {
            "running": running,
            "paused": paused,
            "oldest_age_seconds": max(0.0, generated_at - min(run_started)) if run_started else None,
            "runs": runs,
            "unavailable": "active_v1_runs" in unread_sources,
        },
        "parity": dict(parity),
        "evidence_errors": unread,
        "rollback_ready": rollback_ready,
        "cutover_eligible": not blocking_reasons,
        "blocking_reasons": blocking_reasons,
    }


# ---------------------------------------------------------------------------
# §5.5 — the release check
# ---------------------------------------------------------------------------

#: Where the reviewed fixtures live, relative to the repository root.
REVIEWED_FIXTURE_ROOT = "tests/fixtures/playbooks/v2"

#: The shipped playbooks whose reviewed evidence Package 6 promises to carry.
#:
#: The fixture root also contains compiler, lowering and event corpora, so
#: "every child directory" is not the completeness boundary.  The Package 6
#: fixture layout (§3.4) locks these four ids instead.  Keeping the set beside
#: the release check means an empty or partially deleted fixture tree cannot
#: turn into a successful check merely because there was nothing to iterate.
EXPECTED_REVIEWED_FIXTURE_IDS: frozenset[str] = frozenset(
    {
        "coding-reflection",
        "default-assignment-routing",
        "default-pipeline",
        "memory-consolidation",
    }
)


@dataclass(frozen=True, slots=True)
class StaleArtifact:
    """One reviewed artifact that no longer matches the surface it compiled against."""

    playbook_id: str
    origin: str                      # "fixture" | "activation"
    kind: str                        # "command" | "profile"
    dependency: str                  # the command or profile that moved
    reviewed_fingerprint: str | None
    current_fingerprint: str | None

    @property
    def change(self) -> str:
        return "removed" if self.current_fingerprint is None else "changed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "origin": self.origin,
            "kind": self.kind,
            "dependency": self.dependency,
            "change": self.change,
            "reviewed_fingerprint": self.reviewed_fingerprint,
            "current_fingerprint": self.current_fingerprint,
            "message": (
                f"{self.playbook_id}: {self.kind} {self.dependency!r} "
                f"{self.change} since the artifact was reviewed; rebuild and re-review"
            ),
        }


@dataclass(frozen=True, slots=True)
class UnverifiedActivation:
    """One enabled activation the release check could **not** compare.

    The gate's claim is "every enabled activation was compared against the live
    contracts".  A row whose artifact is missing, unreadable, or whose profile
    baseline could not be resolved cannot carry that claim, and silently
    skipping it produced the payload of a clean fleet from a daemon that had
    read none of its own activations (`prime-zenith-66`).  Each such row is
    named here instead, and each becomes a blocking reason.

    A *disabled* row never lands here: an operator decided about it, and a
    decision made on purpose is not missing evidence.  An acknowledgement does
    not excuse a row that remains enabled; live execution still needs evidence.
    """

    playbook_id: str
    scope: str
    scope_identifier: str | None
    artifact_sha256: str | None
    reason: str
    detail: str = ""

    @property
    def message(self) -> str:
        subject = self.playbook_id or "an activation row with no playbook id"
        where = f" ({self.scope}:{self.scope_identifier})" if self.scope_identifier else ""
        detail = f" — {self.detail}" if self.detail else ""
        return (
            f"{subject}{where}: enabled activation could not be compared against the "
            f"live contracts [{self.reason}]{detail}; a release cannot be certified "
            "against evidence that was never collected"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "playbook_id": self.playbook_id,
            "scope": self.scope,
            "scope_identifier": self.scope_identifier,
            "artifact_sha256": self.artifact_sha256,
            "reason": self.reason,
            "detail": self.detail,
            "message": self.message,
        }


def _unverified(row: Mapping[str, Any], reason: str, detail: str = "") -> UnverifiedActivation:
    """Build an :class:`UnverifiedActivation` from an activation row.

    *reason* is the fallback; a row that already knows why it is uncomparable
    carries ``evidence_reason``/``evidence_detail``, which the daemon sets when
    the failure happened while collecting the row rather than while comparing
    it.
    """
    scope_identifier = row.get("scope_identifier")
    return UnverifiedActivation(
        playbook_id=str(row.get("playbook_id") or ""),
        scope=str(row.get("scope") or "system"),
        scope_identifier=str(scope_identifier) if scope_identifier else None,
        artifact_sha256=str(row.get("artifact_sha256") or "") or None,
        reason=str(row.get("evidence_reason") or reason),
        detail=str(row.get("evidence_detail") or detail or ""),
    )


def _reviewed_fixture_artifacts(
    fixture_root: Path,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Approved fixture artifacts and any evidence that could not be read.

    A fixture whose ``review.md`` does not record ``decision: approved`` is not
    read: it is a recorded negative, no activation may reference it, and
    holding it to the live contract surface would report drift in something
    nothing runs.

    The four shipped fixture ids are required even when their directories are
    absent.  Additional top-level directories only become fixture candidates
    when they carry ``review.md`` or ``artifact.json``; this deliberately
    ignores the event/lowering/invalid corpora that share the root.
    """
    from src.playbooks.definition import load_definition_json

    artifacts: dict[str, Any] = {}
    errors: list[dict[str, str]] = []

    def unread(source: str, error: str) -> None:
        errors.append({"source": source, "error": error})

    try:
        if not fixture_root.is_dir():
            unread(
                "reviewed_fixtures",
                f"fixture root is missing or is not a directory: {fixture_root}",
            )
            return artifacts, errors
        children = sorted(fixture_root.iterdir())
    except OSError as exc:
        unread("reviewed_fixtures", f"cannot list {fixture_root}: {exc}")
        return artifacts, errors

    directories = {path.name: path for path in children if path.is_dir()}
    candidates = set(EXPECTED_REVIEWED_FIXTURE_IDS)
    for directory in directories.values():
        if (directory / "review.md").exists() or (directory / "artifact.json").exists():
            candidates.add(directory.name)

    for playbook_id in sorted(candidates):
        source = f"reviewed_fixture:{playbook_id}"
        directory = directories.get(playbook_id)
        if directory is None:
            unread(
                source,
                f"required fixture directory is missing: {fixture_root / playbook_id}",
            )
            continue
        artifact_path = directory / "artifact.json"
        review_path = directory / "review.md"
        try:
            artifact_present = artifact_path.is_file()
            review_present = review_path.is_file()
        except OSError as exc:
            unread(source, f"cannot inspect fixture files in {directory}: {exc}")
            continue

        missing = [
            name
            for name, present in (("artifact.json", artifact_present), ("review.md", review_present))
            if not present
        ]
        if missing:
            unread(source, f"incomplete fixture; missing {', '.join(missing)}")
            continue

        try:
            frontmatter = _review_frontmatter(review_path)
        except Exception as exc:
            unread(source, f"review.md is unreadable or malformed: {exc}")
            continue
        if not frontmatter:
            unread(source, "review.md is malformed or has no YAML frontmatter mapping")
            continue
        if frontmatter.get("decision") != "approved":
            if playbook_id in EXPECTED_REVIEWED_FIXTURE_IDS:
                unread(source, "required shipped fixture is not approved")
            continue
        reviewed_id = str(frontmatter.get("playbook_id") or "")
        if reviewed_id != playbook_id:
            unread(
                source,
                f"review.md playbook_id {reviewed_id!r} does not match directory {playbook_id!r}",
            )
            continue
        try:
            definition = load_definition_json(artifact_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("release check: unreadable fixture %s: %s", artifact_path, exc)
            unread(source, f"artifact.json is unreadable or malformed: {exc}")
            continue
        if definition.id != playbook_id:
            unread(
                source,
                f"artifact.json playbook id {definition.id!r} does not match directory {playbook_id!r}",
            )
            continue
        artifacts[playbook_id] = definition
    return artifacts, errors


def reviewed_artifact_evidence(
    fixture_root: Path | str = REVIEWED_FIXTURE_ROOT,
) -> dict[str, dict[str, Any]]:
    """``{playbook_id: review frontmatter}`` for every *approved* reviewed fixture.

    The human decision record is the only place ``reviewed_by`` and
    ``reviewed_at`` exist — no table stores them (§3.4) — so the cutover report
    reads them from the same files the release check reads artifacts from.

    Only ``decision: approved`` is returned.  A rejected or undecided review is
    not weaker evidence than none, it is evidence of the opposite, and the
    report's caller must not be able to mistake one for the other.
    """
    root = Path(fixture_root)
    evidence: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return evidence
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        review_path = directory / "review.md"
        if not review_path.is_file():
            continue
        try:
            frontmatter = _review_frontmatter(review_path)
        except Exception:  # pragma: no cover - a malformed review is a test failure
            logger.warning("cutover report: unreadable review %s", review_path, exc_info=True)
            continue
        if frontmatter.get("decision") != "approved":
            continue
        evidence[str(frontmatter.get("playbook_id") or directory.name)] = frontmatter
    return evidence


def _review_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 3)
    if end == -1:
        return {}
    parsed = yaml.safe_load(text[4:end])
    return parsed if isinstance(parsed, dict) else {}


def _compare_fingerprints(
    playbook_id: str,
    origin: str,
    kind: str,
    reviewed: Mapping[str, str],
    current: Mapping[str, str],
) -> list[StaleArtifact]:
    stale: list[StaleArtifact] = []
    for dependency, reviewed_fingerprint in sorted(reviewed.items()):
        current_fingerprint = current.get(dependency)
        if current_fingerprint == reviewed_fingerprint:
            continue
        stale.append(
            StaleArtifact(
                playbook_id=playbook_id,
                origin=origin,
                kind=kind,
                dependency=dependency,
                reviewed_fingerprint=str(reviewed_fingerprint),
                current_fingerprint=(
                    None if current_fingerprint is None else str(current_fingerprint)
                ),
            )
        )
    return stale


def current_command_fingerprints(contract_registry: Any) -> dict[str, str]:
    """``{command: execution fingerprint}`` for everything the registry serves."""
    fingerprints: dict[str, str] = {}
    for name in sorted(contract_registry.names()):
        registration = contract_registry.get(name)
        if registration is None:  # pragma: no cover - names() and get() disagree
            continue
        fingerprints[name] = str(registration.contract.fingerprint())
    return fingerprints


def shipped_profile_lookup(root: str | None = None) -> Any:
    """A ``ProfileLookup`` over the profiles **this repository ships**.

    Production resolves profiles from the database, which carries an
    operator's edits.  A reviewed fixture must not depend on one operator's
    install, so it is compiled — and held — against
    ``src/profiles/defaults/<id>/profile.md``.  That is why the release check
    and ``scripts/rebuild-reviewed-playbook-artifacts.py`` share this
    construction instead of each growing their own.

    Offline: it reads Markdown off disk and builds capability policies from
    it.  A shipped profile that no longer parses raises, because a release
    check that quietly skipped it would report the profile as *removed* and
    name the wrong cause.
    """
    from types import SimpleNamespace

    from src.playbooks.validation import VaultProfileLookup
    from src.profiles.drift import defaults_root, shipped_profile_path, system_profile_ids
    from src.profiles.parser import parse_profile, parsed_profile_to_agent_profile

    base = root or defaults_root()
    profiles: dict[str, Any] = {}
    for profile_id in system_profile_ids(base):
        path = shipped_profile_path(profile_id, base)
        parsed = parse_profile(Path(path).read_text(encoding="utf-8"))
        if not parsed.is_valid:
            raise ValueError(f"shipped profile {path} does not parse: {parsed.errors}")
        fields = parsed_profile_to_agent_profile(parsed)
        profiles[fields["id"]] = SimpleNamespace(**fields)
    return VaultProfileLookup(profiles)


def profile_fingerprints_for(profile_lookup: Any, profile_ids: Iterable[str]) -> dict[str, str]:
    """``{profile_id: capability fingerprint}`` for the ids the lookup resolves.

    An unresolvable id is **omitted**, which is the same convention
    ``load_activation_health`` uses: a missing entry reads as "removed", and
    inventing a placeholder would make a removal look like a change.
    """
    fingerprints: dict[str, str] = {}
    for profile_id in profile_ids:
        policy = profile_lookup.policy(profile_id)
        if policy is not None:
            fingerprints[profile_id] = str(policy.fingerprint())
    return fingerprints


def shipped_profile_fingerprints(root: str | None = None) -> dict[str, str]:
    """Capability fingerprints for every shipped system profile, by id."""
    from src.profiles.drift import defaults_root, system_profile_ids

    base = root or defaults_root()
    return profile_fingerprints_for(shipped_profile_lookup(base), system_profile_ids(base))


def release_check(
    *,
    contract_registry: Any,
    fixture_root: Path | str = REVIEWED_FIXTURE_ROOT,
    profile_fingerprints: Mapping[str, str] | None = None,
    activations: Sequence[Mapping[str, Any]] = (),
    evidence_errors: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Do the reviewed artifacts still match the surface they were compiled against?

    §5.5's release gate, and deliberately **offline**: it performs no network
    call, no LLM call and no compile.  It reads the checked-in fixtures and,
    when given them, the enabled activation rows, and compares each artifact's
    ``compiled_against`` against the in-process registries — both halves of it,
    commands *and* capability profiles.

    A changed *execution* fingerprint is drift; a presentation-only label change
    is not, because the fingerprint is taken over the execution contract alone
    (``src/commands/contracts/models.py::execution_fingerprint``).  A changed
    *capability* fingerprint is drift for the same reason: the artifact was
    approved on the strength of what those profiles were allowed to do.

    *profile_fingerprints* is the map the fixtures are held to, and defaults to
    :func:`shipped_profile_fingerprints` — the profiles this build ships, which
    are the ones a fixture was compiled against.  It is a parameter only so a
    test can perturb one.

    *activations* are mappings shaped like the rows
    ``playbook_migration_queries`` returns.  A row that is **disabled** does
    not block: an operator has already decided about it, and a decision made on
    purpose is not a regression.  An acknowledgement does *not* excuse an
    enabled row.  The waiver says "this playbook is not migrating"; an enabled
    activation says it is already running V2.  Both cannot be true, and
    honouring the waiver anyway let a stale artifact keep executing while this
    gate reported a clean fleet (``sound-horizon-20``).  An operator who means
    the waiver disables the activation, and *that* is the state this gate
    reads.  A row may carry its own ``current_profiles`` map — the daemon
    supplies one resolved from its *live* profile registry, because that, not
    the shipped defaults, is what its artifacts were compiled against.

    Every **other** enabled row is either compared or named.  A row with no
    usable ``artifact_commands`` — a missing artifact, an unreadable one, an
    unavailable store — becomes an :class:`UnverifiedActivation` in
    ``unverified``, and one whose ``current_profiles_unavailable`` flag is set
    has its profile half reported unverified rather than silently held to the
    shipped defaults, which are a *different* baseline.

    *evidence_errors* are the reads the caller could not perform at all, one
    ``{"source": ..., "error": ...}`` mapping each, exactly as
    :func:`build_cutover_report` takes them.  They exist because an unread
    source and an empty one are not the same fact: an activation query that
    failed and was rendered as zero activations let this gate certify a fleet
    nobody looked at.

    The fixture read contributes errors in the same shape.  The Package 6
    fixture layout's four shipped playbooks are an expected set, so a missing
    root, an empty/partial tree, or malformed approved evidence is named and
    blocks instead of collapsing to an empty successful comparison.

    Anything in ``evidence_errors`` or ``unverified`` therefore appears in
    ``blocking_reasons``, and a non-empty ``blocking_reasons`` fails the check
    just as a stale artifact does.
    """
    fixture_root = Path(fixture_root)
    current_commands = current_command_fingerprints(contract_registry)
    current_profiles = (
        dict(profile_fingerprints)
        if profile_fingerprints is not None
        else shipped_profile_fingerprints()
    )
    stale: list[StaleArtifact] = []
    unverified: list[UnverifiedActivation] = []
    checked: list[str] = []
    unread = [
        {
            "source": str(row.get("source") or "unknown"),
            "error": str(row.get("error") or "unavailable"),
        }
        for row in evidence_errors
    ]

    fixture_artifacts, fixture_errors = _reviewed_fixture_artifacts(fixture_root)
    unread.extend(fixture_errors)
    for playbook_id, definition in fixture_artifacts.items():
        checked.append(playbook_id)
        compiled = definition.compiled_against
        stale += _compare_fingerprints(
            playbook_id, "fixture", "command", compiled.commands, current_commands
        )
        stale += _compare_fingerprints(
            playbook_id, "fixture", "profile", compiled.profiles, current_profiles
        )

    for row in activations:
        # Only a disabled activation is excused.  `acknowledged_by` is
        # deliberately *not* consulted here: a waiver recorded against a
        # playbook whose activation is still enabled describes a state that
        # does not exist, and skipping on it suppressed the compatibility
        # check for live execution (`sound-horizon-20`).
        if not row.get("enabled", True):
            continue
        playbook_id = str(row.get("playbook_id") or "")
        commands = row.get("artifact_commands") or {}
        if not playbook_id:
            unverified.append(
                _unverified(row, "missing_playbook_id", "the activation row names no playbook")
            )
            continue
        if not commands:
            unverified.append(_unverified(row, "no_command_evidence"))
            continue
        checked.append(playbook_id)
        stale += _compare_fingerprints(
            playbook_id, "activation", "command", commands, current_commands
        )
        profiles = row.get("artifact_profiles")
        if profiles:
            if row.get("current_profiles_unavailable"):
                unverified.append(_unverified(row, "profile_registry_unavailable"))
            else:
                row_current = row.get("current_profiles")
                stale += _compare_fingerprints(
                    playbook_id,
                    "activation",
                    "profile",
                    profiles,
                    current_profiles if row_current is None else dict(row_current),
                )

    blocking_reasons = [
        f"evidence source {row['source']!r} could not be read ({row['error']}); "
        "a release cannot be certified against evidence that was never collected"
        for row in unread
    ]
    blocking_reasons += [entry.message for entry in unverified]

    return {
        "success": not stale and not blocking_reasons,
        "checked": sorted(set(checked)),
        "registry_fingerprint": str(contract_registry.registry_fingerprint()),
        "stale": [entry.to_dict() for entry in stale],
        "unverified": [entry.to_dict() for entry in unverified],
        "evidence_errors": unread,
        "blocking_reasons": blocking_reasons,
    }


__all__ = [
    "REASON_CODES",
    "REVIEWED_FIXTURE_ROOT",
    "EXPECTED_REVIEWED_FIXTURE_IDS",
    "SHA256_RE",
    "EXPECTED_DIFFERENCES",
    "AuthzDecision",
    "CapabilityFinding",
    "CommandInvocation",
    "InventoryEntry",
    "MigrationInventory",
    "MigrationReason",
    "MigrationReasonError",
    "ParityFinding",
    "PlaybookDisposition",
    "ShadowObservation",
    "SourceRef",
    "StaleArtifact",
    "UnverifiedActivation",
    "audit_capabilities",
    "build_inventory",
    "build_cutover_report",
    "compare",
    "current_command_fingerprints",
    "find_embedded_action_block",
    "profile_fingerprints_for",
    "release_check",
    "required_capabilities",
    "reviewed_artifact_evidence",
    "shipped_profile_fingerprints",
    "shipped_profile_lookup",
]
