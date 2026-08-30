from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AddDependencyRequest")


@_attrs_define
class AddDependencyRequest:
    """
    Attributes:
        task_id (str): The task that should wait (downstream task)
        depends_on (str): The task that must complete first (upstream task)
        dep_type (None | str | Unset): Edge kind (default 'blocks'). Blocking kinds gate readiness: 'blocks' waits for
            completion, 'parent-child' marks a container that withholds its children until released, 'waits-for' fans in
            over a container's children, 'conditional-blocks' runs only if the dependency terminally failed. The rest are
            provenance only and never block.
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

        add_dependency_request = cls(
            task_id=task_id,
            depends_on=depends_on,
            dep_type=dep_type,
        )

        add_dependency_request.additional_properties = d
        return add_dependency_request

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
