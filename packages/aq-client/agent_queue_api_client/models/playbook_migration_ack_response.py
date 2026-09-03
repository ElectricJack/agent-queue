from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.migration_ack_dto import MigrationAckDTO


T = TypeVar("T", bound="PlaybookMigrationAckResponse")


@_attrs_define
class PlaybookMigrationAckResponse:
    """
    Attributes:
        success (bool):
        acknowledgement (MigrationAckDTO | None | Unset):
        playbook_id (None | str | Unset):
        removed (int | None | Unset):
        error (None | str | Unset):
    """

    success: bool
    acknowledgement: MigrationAckDTO | None | Unset = UNSET
    playbook_id: None | str | Unset = UNSET
    removed: int | None | Unset = UNSET
    error: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.migration_ack_dto import MigrationAckDTO

        success = self.success

        acknowledgement: dict[str, Any] | None | Unset
        if isinstance(self.acknowledgement, Unset):
            acknowledgement = UNSET
        elif isinstance(self.acknowledgement, MigrationAckDTO):
            acknowledgement = self.acknowledgement.to_dict()
        else:
            acknowledgement = self.acknowledgement

        playbook_id: None | str | Unset
        if isinstance(self.playbook_id, Unset):
            playbook_id = UNSET
        else:
            playbook_id = self.playbook_id

        removed: int | None | Unset
        if isinstance(self.removed, Unset):
            removed = UNSET
        else:
            removed = self.removed

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "success": success,
            }
        )
        if acknowledgement is not UNSET:
            field_dict["acknowledgement"] = acknowledgement
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if removed is not UNSET:
            field_dict["removed"] = removed
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.migration_ack_dto import MigrationAckDTO

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_acknowledgement(data: object) -> MigrationAckDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                acknowledgement_type_0 = MigrationAckDTO.from_dict(data)

                return acknowledgement_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(MigrationAckDTO | None | Unset, data)

        acknowledgement = _parse_acknowledgement(d.pop("acknowledgement", UNSET))

        def _parse_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_id = _parse_playbook_id(d.pop("playbook_id", UNSET))

        def _parse_removed(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        removed = _parse_removed(d.pop("removed", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        playbook_migration_ack_response = cls(
            success=success,
            acknowledgement=acknowledgement,
            playbook_id=playbook_id,
            removed=removed,
            error=error,
        )

        return playbook_migration_ack_response
