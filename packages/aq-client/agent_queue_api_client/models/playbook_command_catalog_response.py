from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

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
    """

    success: bool | Unset = True
    commands: list[PlaybookCommandCatalogEntryDTO] | Unset = UNSET
    count: int | Unset = 0

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        commands: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.commands, Unset):
            commands = []
            for commands_item_data in self.commands:
                commands_item = commands_item_data.to_dict()
                commands.append(commands_item)

        count = self.count

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if commands is not UNSET:
            field_dict["commands"] = commands
        if count is not UNSET:
            field_dict["count"] = count

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

        playbook_command_catalog_response = cls(
            success=success,
            commands=commands,
            count=count,
        )

        return playbook_command_catalog_response
