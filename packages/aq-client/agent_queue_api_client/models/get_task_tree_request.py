from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GetTaskTreeRequest")


@_attrs_define
class GetTaskTreeRequest:
    """
    Attributes:
        task_id (str): The root/parent task ID whose subtree to display
        compact (bool | None | Unset): When true, show only the root task with a subtask count and progress bar instead
            of the full expanded tree. Default false (full tree).
        max_depth (int | None | Unset): Maximum nesting depth to render (default 4). Deeper subtasks are collapsed into
            a summary.
    """

    task_id: str
    compact: bool | None | Unset = UNSET
    max_depth: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        compact: bool | None | Unset
        if isinstance(self.compact, Unset):
            compact = UNSET
        else:
            compact = self.compact

        max_depth: int | None | Unset
        if isinstance(self.max_depth, Unset):
            max_depth = UNSET
        else:
            max_depth = self.max_depth

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if compact is not UNSET:
            field_dict["compact"] = compact
        if max_depth is not UNSET:
            field_dict["max_depth"] = max_depth

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        def _parse_compact(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        compact = _parse_compact(d.pop("compact", UNSET))

        def _parse_max_depth(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_depth = _parse_max_depth(d.pop("max_depth", UNSET))

        get_task_tree_request = cls(
            task_id=task_id,
            compact=compact,
            max_depth=max_depth,
        )

        get_task_tree_request.additional_properties = d
        return get_task_tree_request

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
