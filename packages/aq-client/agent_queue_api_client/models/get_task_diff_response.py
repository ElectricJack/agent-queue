from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GetTaskDiffResponse")


@_attrs_define
class GetTaskDiffResponse:
    """
    Attributes:
        diff (str | Unset):  Default: ''.
        branch (str | Unset):  Default: ''.
    """

    diff: str | Unset = ""
    branch: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        diff = self.diff

        branch = self.branch

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if diff is not UNSET:
            field_dict["diff"] = diff
        if branch is not UNSET:
            field_dict["branch"] = branch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        diff = d.pop("diff", UNSET)

        branch = d.pop("branch", UNSET)

        get_task_diff_response = cls(
            diff=diff,
            branch=branch,
        )

        get_task_diff_response.additional_properties = d
        return get_task_diff_response

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
