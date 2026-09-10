from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.command_center_project_view_manual_positions import CommandCenterProjectViewManualPositions


T = TypeVar("T", bound="CommandCenterProjectView")


@_attrs_define
class CommandCenterProjectView:
    """
    Attributes:
        expanded_task_ids (list[str] | Unset):
        expanded_finished_task_ids (list[str] | Unset):
        manual_positions (CommandCenterProjectViewManualPositions | Unset):
    """

    expanded_task_ids: list[str] | Unset = UNSET
    expanded_finished_task_ids: list[str] | Unset = UNSET
    manual_positions: CommandCenterProjectViewManualPositions | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        expanded_task_ids: list[str] | Unset = UNSET
        if not isinstance(self.expanded_task_ids, Unset):
            expanded_task_ids = self.expanded_task_ids

        expanded_finished_task_ids: list[str] | Unset = UNSET
        if not isinstance(self.expanded_finished_task_ids, Unset):
            expanded_finished_task_ids = self.expanded_finished_task_ids

        manual_positions: dict[str, Any] | Unset = UNSET
        if not isinstance(self.manual_positions, Unset):
            manual_positions = self.manual_positions.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if expanded_task_ids is not UNSET:
            field_dict["expanded_task_ids"] = expanded_task_ids
        if expanded_finished_task_ids is not UNSET:
            field_dict["expanded_finished_task_ids"] = expanded_finished_task_ids
        if manual_positions is not UNSET:
            field_dict["manual_positions"] = manual_positions

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.command_center_project_view_manual_positions import CommandCenterProjectViewManualPositions

        d = dict(src_dict)
        expanded_task_ids = cast(list[str], d.pop("expanded_task_ids", UNSET))

        expanded_finished_task_ids = cast(list[str], d.pop("expanded_finished_task_ids", UNSET))

        _manual_positions = d.pop("manual_positions", UNSET)
        manual_positions: CommandCenterProjectViewManualPositions | Unset
        if isinstance(_manual_positions, Unset):
            manual_positions = UNSET
        else:
            manual_positions = CommandCenterProjectViewManualPositions.from_dict(_manual_positions)

        command_center_project_view = cls(
            expanded_task_ids=expanded_task_ids,
            expanded_finished_task_ids=expanded_finished_task_ids,
            manual_positions=manual_positions,
        )

        return command_center_project_view
