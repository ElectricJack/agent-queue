from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProviderAllocationPinnedTask")


@_attrs_define
class ProviderAllocationPinnedTask:
    """An explicit pin on a changed profile; allocation never rewrites it.

    Attributes:
        task_id (str):
        profile_id (str):
        status (str):
        project_id (None | str | Unset):
        waits (bool | Unset):  Default: False.
    """

    task_id: str
    profile_id: str
    status: str
    project_id: None | str | Unset = UNSET
    waits: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        profile_id = self.profile_id

        status = self.status

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        waits = self.waits

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "profile_id": profile_id,
                "status": status,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if waits is not UNSET:
            field_dict["waits"] = waits

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        profile_id = d.pop("profile_id")

        status = d.pop("status")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        waits = d.pop("waits", UNSET)

        provider_allocation_pinned_task = cls(
            task_id=task_id,
            profile_id=profile_id,
            status=status,
            project_id=project_id,
            waits=waits,
        )

        provider_allocation_pinned_task.additional_properties = d
        return provider_allocation_pinned_task

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
