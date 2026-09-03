from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.migration_disposition_counts_dto import MigrationDispositionCountsDTO
    from ..models.migration_inventory_entry_dto import MigrationInventoryEntryDTO


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

        d = dict(src_dict)
        success = d.pop("success")

        generated_at = d.pop("generated_at")

        contract_fingerprint = d.pop("contract_fingerprint")

        counts = MigrationDispositionCountsDTO.from_dict(d.pop("counts"))

        blocking = d.pop("blocking")

        pending_events_total = d.pop("pending_events_total")

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
            entries=entries,
            filtered_by=filtered_by,
            error=error,
        )

        return playbook_migration_inventory_response
