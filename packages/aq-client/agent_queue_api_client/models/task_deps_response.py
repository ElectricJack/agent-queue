from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_ref import TaskRef


T = TypeVar("T", bound="TaskDepsResponse")


@_attrs_define
class TaskDepsResponse:
    """
    Attributes:
        task_id (str):
        title (str):
        status (str | Unset):  Default: ''.
        depends_on (list[TaskRef] | Unset):
        blocks (list[TaskRef] | Unset):
    """

    task_id: str
    title: str
    status: str | Unset = ""
    depends_on: list[TaskRef] | Unset = UNSET
    blocks: list[TaskRef] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        title = self.title

        status = self.status

        depends_on: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.depends_on, Unset):
            depends_on = []
            for depends_on_item_data in self.depends_on:
                depends_on_item = depends_on_item_data.to_dict()
                depends_on.append(depends_on_item)

        blocks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.blocks, Unset):
            blocks = []
            for blocks_item_data in self.blocks:
                blocks_item = blocks_item_data.to_dict()
                blocks.append(blocks_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "title": title,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if depends_on is not UNSET:
            field_dict["depends_on"] = depends_on
        if blocks is not UNSET:
            field_dict["blocks"] = blocks

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_ref import TaskRef

        d = dict(src_dict)
        task_id = d.pop("task_id")

        title = d.pop("title")

        status = d.pop("status", UNSET)

        _depends_on = d.pop("depends_on", UNSET)
        depends_on: list[TaskRef] | Unset = UNSET
        if _depends_on is not UNSET:
            depends_on = []
            for depends_on_item_data in _depends_on:
                depends_on_item = TaskRef.from_dict(depends_on_item_data)

                depends_on.append(depends_on_item)

        _blocks = d.pop("blocks", UNSET)
        blocks: list[TaskRef] | Unset = UNSET
        if _blocks is not UNSET:
            blocks = []
            for blocks_item_data in _blocks:
                blocks_item = TaskRef.from_dict(blocks_item_data)

                blocks.append(blocks_item)

        task_deps_response = cls(
            task_id=task_id,
            title=title,
            status=status,
            depends_on=depends_on,
            blocks=blocks,
        )

        task_deps_response.additional_properties = d
        return task_deps_response

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
