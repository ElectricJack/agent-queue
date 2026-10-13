from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_command_catalog_entry_dto import PlaybookCommandCatalogEntryDTO


T = TypeVar("T", bound="PlaybookCommandCatalogResponse")


@_attrs_define
class PlaybookCommandCatalogResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        commands (list[PlaybookCommandCatalogEntryDTO] | Unset):
        count (int | Unset):  Default: 0.
        total (int | Unset):  Default: 0.
        offset (int | Unset):  Default: 0.
        next_offset (int | None | Unset):
    """

    success: bool | Unset = True
    commands: list[PlaybookCommandCatalogEntryDTO] | Unset = UNSET
    count: int | Unset = 0
    total: int | Unset = 0
    offset: int | Unset = 0
    next_offset: int | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        commands: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.commands, Unset):
            commands = []
            for commands_item_data in self.commands:
                commands_item = commands_item_data.to_dict()
                commands.append(commands_item)

        count = self.count

        total = self.total

        offset = self.offset

        next_offset: int | None | Unset
        if isinstance(self.next_offset, Unset):
            next_offset = UNSET
        else:
            next_offset = self.next_offset

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if commands is not UNSET:
            field_dict["commands"] = commands
        if count is not UNSET:
            field_dict["count"] = count
        if total is not UNSET:
            field_dict["total"] = total
        if offset is not UNSET:
            field_dict["offset"] = offset
        if next_offset is not UNSET:
            field_dict["next_offset"] = next_offset

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_command_catalog_entry_dto import PlaybookCommandCatalogEntryDTO

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _commands = d.pop("commands", UNSET)
        commands: list[PlaybookCommandCatalogEntryDTO] | Unset = UNSET
        if _commands is not UNSET:
            commands = []
            for commands_item_data in _commands:
                commands_item = PlaybookCommandCatalogEntryDTO.from_dict(commands_item_data)

                commands.append(commands_item)

        count = d.pop("count", UNSET)

        total = d.pop("total", UNSET)

        offset = d.pop("offset", UNSET)

        def _parse_next_offset(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        next_offset = _parse_next_offset(d.pop("next_offset", UNSET))

        playbook_command_catalog_response = cls(
            success=success,
            commands=commands,
            count=count,
            total=total,
            offset=offset,
            next_offset=next_offset,
        )

        return playbook_command_catalog_response
