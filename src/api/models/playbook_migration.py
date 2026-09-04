"""Typed response models for the Playbook V1→V2 migration readiness surface.

Package 6 of the Playbook V2 roadmap
(``docs/superpowers/plans/2026-09-01-playbook-v2-migration-artifacts.md`` §3.6).

Kept out of ``playbook_v2.py`` on purpose: that module is Package 5's frozen §4
interface contract and its registration block is asserted to hold exactly the
Package 2 and 5 command surfaces.  Package 6 adds commands to the same ``aq
playbook`` CLI group, not to that contract.

Conventions match ``playbook_v2.py``: strict models, full ``sha256:<64 hex>``
hashes, POSIX-second floats.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from src.api.models.playbook_v2 import ArtifactRefDTO, V2Model


class MigrationReasonDTO(V2Model):
    """One operator-facing explanation for an entry's disposition.

    ``code`` is drawn from ``src.playbooks.migration.REASON_CODES``, a closed
    set the CLI and the cutover report both switch on.
    """

    code: str
    message: str
    source_line: int | None = None


class MigrationSourceRefDTO(V2Model):
    """Where an inventory entry's authoring Markdown lives.

    Distinct from :class:`SourceRefDTO`, which points at a span *inside* a
    source; this points at the whole file and carries its content hash.
    """

    vault_rel_path: str
    bundled_rel_path: str | None = None
    source_sha256: str


class MigrationInventoryEntryDTO(V2Model):
    playbook_id: str
    scope: str
    scope_identifier: str | None = None
    source: MigrationSourceRefDTO
    v1_kind: str
    v1_version: int | None = None
    v1_enabled: bool
    disposition: Literal["ready", "question_required", "invalid", "disabled"]
    reasons: list[MigrationReasonDTO] = []
    artifact: ArtifactRefDTO | None = None
    activation_health: str | None = None
    has_embedded_action_block: bool
    acknowledged_by: str | None = None
    acknowledged_at: float | None = None
    pending_events: int = 0


class MigrationDispositionCountsDTO(V2Model):
    ready: int
    question_required: int
    invalid: int
    disabled: int


class PlaybookMigrationInventoryResponse(V2Model):
    success: bool
    generated_at: float
    contract_fingerprint: str
    counts: MigrationDispositionCountsDTO
    #: Entries that stand between the fleet and cutover: everything not
    #: ``ready``, minus acknowledged and frontmatter-disabled playbooks.
    blocking: int
    pending_events_total: int
    #: False when an evidence source could not be read.  The counts and
    #: ``blocking`` above are then computed against evidence that was never
    #: collected, so a zero ``blocking`` does not mean the fleet can cut over.
    evidence_complete: bool = True
    #: The reads that failed, one ``{"source", "error"}`` each.
    evidence_errors: list[dict[str, Any]] = []
    #: One reason per unread evidence source; empty when the report is complete.
    blocking_reasons: list[str] = []
    entries: list[MigrationInventoryEntryDTO] = []
    #: Present only when the caller filtered; the counts above always describe
    #: the whole fleet regardless.
    filtered_by: str | None = None
    error: str | None = None


class MigrationAckDTO(V2Model):
    playbook_id: str
    scope: str
    scope_identifier: str
    source_sha256: str
    reason: str
    #: Server-derived from the execution principal, never from the request body.
    acknowledged_by: str
    acknowledged_at: float


class PlaybookMigrationAckResponse(V2Model):
    success: bool
    acknowledgement: MigrationAckDTO | None = None
    playbook_id: str | None = None
    removed: int | None = None
    error: str | None = None


class StaleArtifactDTO(V2Model):
    """One reviewed artifact whose compiled-against surface has moved."""

    playbook_id: str
    #: ``fixture`` (a checked-in reviewed artifact) or ``activation`` (a row).
    origin: Literal["fixture", "activation"]
    kind: Literal["command", "profile"]
    dependency: str
    change: Literal["changed", "removed"]
    reviewed_fingerprint: str | None = None
    current_fingerprint: str | None = None
    message: str


class PlaybookReleaseCheckResponse(V2Model):
    """Whether every reviewed artifact still matches the live command surface."""

    success: bool
    #: Playbook ids compared, fixtures and enabled activations together.
    checked: list[str] = []
    registry_fingerprint: str | None = None
    stale: list[StaleArtifactDTO] = []
    #: Enabled activations that could **not** be compared — a missing or
    #: unreadable artifact, an unavailable store, an unresolvable profile
    #: baseline.  Each names the playbook and why, because a skipped activation
    #: is not a clean one.
    unverified: list[dict[str, Any]] = []
    #: Live evidence the daemon could not read, one ``{"source", "error"}``
    #: each: an unread activation query is not an empty fleet.
    evidence_errors: list[dict[str, Any]] = []
    #: Every reason the check fails that is *not* contract drift.  Non-empty
    #: means ``success`` is false.
    blocking_reasons: list[str] = []
    error: str | None = None


class PlaybookCutoverReportResponse(V2Model):
    """Read-only, signed-operator-ready evidence for the V1→V2 cutover."""

    success: bool
    generated_at: float
    contract_fingerprint: str
    artifacts: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    acknowledged_disabled: list[dict[str, Any]] = []
    pending_events: dict[str, Any]
    active_v1_runs: dict[str, Any]
    parity: dict[str, Any]
    #: Evidence the daemon could not read, one ``{"source", "error"}`` each.
    #: Non-empty means the report is incomplete, so every entry is also a
    #: blocking reason: an unread source is not a clean one.
    evidence_errors: list[dict[str, Any]] = []
    rollback_ready: bool
    cutover_eligible: bool
    blocking_reasons: list[str] = []


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "playbook_migration_inventory": PlaybookMigrationInventoryResponse,
    "playbook_migration_acknowledge": PlaybookMigrationAckResponse,
    "playbook_migration_unacknowledge": PlaybookMigrationAckResponse,
    "playbook_release_check": PlaybookReleaseCheckResponse,
    "playbook_cutover_report": PlaybookCutoverReportResponse,
}
