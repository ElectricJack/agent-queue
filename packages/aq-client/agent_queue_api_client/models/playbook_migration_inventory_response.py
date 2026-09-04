from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.migration_disposition_counts_dto import MigrationDispositionCountsDTO
    from ..models.migration_inventory_entry_dto import MigrationInventoryEntryDTO
    from ..models.playbook_migration_inventory_response_evidence_errors_item import (
        PlaybookMigrationInventoryResponseEvidenceErrorsItem,
    )


T = TypeVar("T", bound="PlaybookMigrationInventoryResponse")


@_attrs_define
class PlaybookMigrationInventoryResponse:
    """
    Attributes:
        success (bool):
        generated_at (float):
        contract_fingerprint (str):
        counts (MigrationDispositionCountsDTO):
        blocking (int):
        pending_events_total (int):
        evidence_complete (bool | Unset):  Default: True.
        evidence_errors (list[PlaybookMigrationInventoryResponseEvidenceErrorsItem] | Unset):
        blocking_reasons (list[str] | Unset):
        entries (list[MigrationInventoryEntryDTO] | Unset):
        filtered_by (None | str | Unset):
        error (None | str | Unset):
    """

    success: bool
    generated_at: float
    contract_fingerprint: str
    counts: MigrationDispositionCountsDTO
    blocking: int
    pending_events_total: int
    evidence_complete: bool | Unset = True
    evidence_errors: list[PlaybookMigrationInventoryResponseEvidenceErrorsItem] | Unset = UNSET
    blocking_reasons: list[str] | Unset = UNSET
    entries: list[MigrationInventoryEntryDTO] | Unset = UNSET
    filtered_by: None | str | Unset = UNSET
    error: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        generated_at = self.generated_at

        contract_fingerprint = self.contract_fingerprint

        counts = self.counts.to_dict()

        blocking = self.blocking

        pending_events_total = self.pending_events_total

        evidence_complete = self.evidence_complete

        evidence_errors: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.evidence_errors, Unset):
            evidence_errors = []
            for evidence_errors_item_data in self.evidence_errors:
                evidence_errors_item = evidence_errors_item_data.to_dict()
                evidence_errors.append(evidence_errors_item)

        blocking_reasons: list[str] | Unset = UNSET
        if not isinstance(self.blocking_reasons, Unset):
            blocking_reasons = self.blocking_reasons

        entries: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.entries, Unset):
            entries = []
            for entries_item_data in self.entries:
                entries_item = entries_item_data.to_dict()
                entries.append(entries_item)

        filtered_by: None | str | Unset
        if isinstance(self.filtered_by, Unset):
            filtered_by = UNSET
        else:
            filtered_by = self.filtered_by

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "success": success,
                "generated_at": generated_at,
                "contract_fingerprint": contract_fingerprint,
                "counts": counts,
                "blocking": blocking,
                "pending_events_total": pending_events_total,
            }
        )
        if evidence_complete is not UNSET:
            field_dict["evidence_complete"] = evidence_complete
        if evidence_errors is not UNSET:
            field_dict["evidence_errors"] = evidence_errors
        if blocking_reasons is not UNSET:
            field_dict["blocking_reasons"] = blocking_reasons
        if entries is not UNSET:
            field_dict["entries"] = entries
        if filtered_by is not UNSET:
            field_dict["filtered_by"] = filtered_by
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.migration_disposition_counts_dto import MigrationDispositionCountsDTO
        from ..models.migration_inventory_entry_dto import MigrationInventoryEntryDTO
        from ..models.playbook_migration_inventory_response_evidence_errors_item import (
            PlaybookMigrationInventoryResponseEvidenceErrorsItem,
        )

        d = dict(src_dict)
        success = d.pop("success")

        generated_at = d.pop("generated_at")

        contract_fingerprint = d.pop("contract_fingerprint")

        counts = MigrationDispositionCountsDTO.from_dict(d.pop("counts"))

        blocking = d.pop("blocking")

        pending_events_total = d.pop("pending_events_total")

        evidence_complete = d.pop("evidence_complete", UNSET)

        _evidence_errors = d.pop("evidence_errors", UNSET)
        evidence_errors: list[PlaybookMigrationInventoryResponseEvidenceErrorsItem] | Unset = UNSET
        if _evidence_errors is not UNSET:
            evidence_errors = []
            for evidence_errors_item_data in _evidence_errors:
                evidence_errors_item = PlaybookMigrationInventoryResponseEvidenceErrorsItem.from_dict(
                    evidence_errors_item_data
                )

                evidence_errors.append(evidence_errors_item)

        blocking_reasons = cast(list[str], d.pop("blocking_reasons", UNSET))

        _entries = d.pop("entries", UNSET)
        entries: list[MigrationInventoryEntryDTO] | Unset = UNSET
        if _entries is not UNSET:
            entries = []
            for entries_item_data in _entries:
                entries_item = MigrationInventoryEntryDTO.from_dict(entries_item_data)

                entries.append(entries_item)

        def _parse_filtered_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        filtered_by = _parse_filtered_by(d.pop("filtered_by", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_migration_inventory_response = cls(
            success=success,
            generated_at=generated_at,
            contract_fingerprint=contract_fingerprint,
            counts=counts,
            blocking=blocking,
            pending_events_total=pending_events_total,
            evidence_complete=evidence_complete,
            evidence_errors=evidence_errors,
            blocking_reasons=blocking_reasons,
            entries=entries,
            filtered_by=filtered_by,
            error=error,
        )

        return playbook_migration_inventory_response
