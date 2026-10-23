from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReparentTaskRequest")


@_attrs_define
class ReparentTaskRequest:
    """
    Attributes:
        task_id (str):
        parent_id (None | str | Unset):
        root (bool | Unset):  Default: False.
        expected_old_generation (int | None | Unset): Hierarchical projects: the old parent's integration generation.
        expected_new_generation (int | None | Unset): Hierarchical projects: the new parent's integration generation.
    """

    task_id: str
    parent_id: None | str | Unset = UNSET
    root: bool | Unset = False
    expected_old_generation: int | None | Unset = UNSET
    expected_new_generation: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        parent_id: None | str | Unset
        if isinstance(self.parent_id, Unset):
            parent_id = UNSET
        else:
            parent_id = self.parent_id

        root = self.root

        expected_old_generation: int | None | Unset
        if isinstance(self.expected_old_generation, Unset):
            expected_old_generation = UNSET
        else:
            expected_old_generation = self.expected_old_generation

        expected_new_generation: int | None | Unset
        if isinstance(self.expected_new_generation, Unset):
            expected_new_generation = UNSET
        else:
            expected_new_generation = self.expected_new_generation

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if parent_id is not UNSET:
            field_dict["parent_id"] = parent_id
        if root is not UNSET:
            field_dict["root"] = root
        if expected_old_generation is not UNSET:
            field_dict["expected_old_generation"] = expected_old_generation
        if expected_new_generation is not UNSET:
            field_dict["expected_new_generation"] = expected_new_generation

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        def _parse_parent_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        parent_id = _parse_parent_id(d.pop("parent_id", UNSET))

        root = d.pop("root", UNSET)

        def _parse_expected_old_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        expected_old_generation = _parse_expected_old_generation(d.pop("expected_old_generation", UNSET))

        def _parse_expected_new_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        expected_new_generation = _parse_expected_new_generation(d.pop("expected_new_generation", UNSET))

        reparent_task_request = cls(
            task_id=task_id,
            parent_id=parent_id,
            root=root,
            expected_old_generation=expected_old_generation,
            expected_new_generation=expected_new_generation,
        )

        reparent_task_request.additional_properties = d
        return reparent_task_request

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
