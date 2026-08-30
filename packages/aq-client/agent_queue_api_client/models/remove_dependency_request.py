from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RemoveDependencyRequest")


@_attrs_define
class RemoveDependencyRequest:
    """
    Attributes:
        task_id (str): The downstream task to unlink
        depends_on (str): The upstream task to remove as a dependency
        dep_type (None | str | Unset): Only remove the edge of this kind. Omit to remove every edge kind between the
            pair.
    """

    task_id: str
    depends_on: str
    dep_type: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        depends_on = self.depends_on

        dep_type: None | str | Unset
        if isinstance(self.dep_type, Unset):
            dep_type = UNSET
        else:
            dep_type = self.dep_type

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "depends_on": depends_on,
            }
        )
        if dep_type is not UNSET:
            field_dict["dep_type"] = dep_type

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        depends_on = d.pop("depends_on")

        def _parse_dep_type(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        dep_type = _parse_dep_type(d.pop("dep_type", UNSET))

        remove_dependency_request = cls(
            task_id=task_id,
            depends_on=depends_on,
            dep_type=dep_type,
        )

        remove_dependency_request.additional_properties = d
        return remove_dependency_request

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
