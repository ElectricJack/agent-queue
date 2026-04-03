from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ToggleProjectHooksResponse")


@_attrs_define
class ToggleProjectHooksResponse:
    """
    Attributes:
        project_id (str):
        action (str | Unset):  Default: ''.
        total_hooks (int | Unset):  Default: 0.
        updated_count (int | Unset):  Default: 0.
        updated_hooks (list[str] | Unset):
    """

    project_id: str
    action: str | Unset = ""
    total_hooks: int | Unset = 0
    updated_count: int | Unset = 0
    updated_hooks: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        action = self.action

        total_hooks = self.total_hooks

        updated_count = self.updated_count

        updated_hooks: list[str] | Unset = UNSET
        if not isinstance(self.updated_hooks, Unset):
            updated_hooks = self.updated_hooks

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if action is not UNSET:
            field_dict["action"] = action
        if total_hooks is not UNSET:
            field_dict["total_hooks"] = total_hooks
        if updated_count is not UNSET:
            field_dict["updated_count"] = updated_count
        if updated_hooks is not UNSET:
            field_dict["updated_hooks"] = updated_hooks

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        action = d.pop("action", UNSET)

        total_hooks = d.pop("total_hooks", UNSET)

        updated_count = d.pop("updated_count", UNSET)

        updated_hooks = cast(list[str], d.pop("updated_hooks", UNSET))

        toggle_project_hooks_response = cls(
            project_id=project_id,
            action=action,
            total_hooks=total_hooks,
            updated_count=updated_count,
            updated_hooks=updated_hooks,
        )

        toggle_project_hooks_response.additional_properties = d
        return toggle_project_hooks_response

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
