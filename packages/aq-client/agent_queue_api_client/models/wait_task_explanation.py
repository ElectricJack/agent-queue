from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitTaskExplanation")


@_attrs_define
class WaitTaskExplanation:
    """
    Attributes:
        id (str):
        lifecycle (str):
        base_priority (int):
        effective_priority (int):
        donor_task_id (None | str | Unset):
        donor_path (list[str] | Unset):
        graph_version (int | None | Unset):
    """

    id: str
    lifecycle: str
    base_priority: int
    effective_priority: int
    donor_task_id: None | str | Unset = UNSET
    donor_path: list[str] | Unset = UNSET
    graph_version: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        lifecycle = self.lifecycle

        base_priority = self.base_priority

        effective_priority = self.effective_priority

        donor_task_id: None | str | Unset
        if isinstance(self.donor_task_id, Unset):
            donor_task_id = UNSET
        else:
            donor_task_id = self.donor_task_id

        donor_path: list[str] | Unset = UNSET
        if not isinstance(self.donor_path, Unset):
            donor_path = self.donor_path

        graph_version: int | None | Unset
        if isinstance(self.graph_version, Unset):
            graph_version = UNSET
        else:
            graph_version = self.graph_version

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "lifecycle": lifecycle,
                "base_priority": base_priority,
                "effective_priority": effective_priority,
            }
        )
        if donor_task_id is not UNSET:
            field_dict["donor_task_id"] = donor_task_id
        if donor_path is not UNSET:
            field_dict["donor_path"] = donor_path
        if graph_version is not UNSET:
            field_dict["graph_version"] = graph_version

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        lifecycle = d.pop("lifecycle")

        base_priority = d.pop("base_priority")

        effective_priority = d.pop("effective_priority")

        def _parse_donor_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        donor_task_id = _parse_donor_task_id(d.pop("donor_task_id", UNSET))

        donor_path = cast(list[str], d.pop("donor_path", UNSET))

        def _parse_graph_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        graph_version = _parse_graph_version(d.pop("graph_version", UNSET))

        wait_task_explanation = cls(
            id=id,
            lifecycle=lifecycle,
            base_priority=base_priority,
            effective_priority=effective_priority,
            donor_task_id=donor_task_id,
            donor_path=donor_path,
            graph_version=graph_version,
        )

        wait_task_explanation.additional_properties = d
        return wait_task_explanation

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
