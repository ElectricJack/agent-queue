from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GitHubIssueTriageResponse")


@_attrs_define
class GitHubIssueTriageResponse:
    """
    Attributes:
        filed (list[int]):
        recovered (list[int]):
        remaining_capacity (int):
        success (bool | Unset):  Default: True.
    """

    filed: list[int]
    recovered: list[int]
    remaining_capacity: int
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        filed = self.filed

        recovered = self.recovered

        remaining_capacity = self.remaining_capacity

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "filed": filed,
                "recovered": recovered,
                "remaining_capacity": remaining_capacity,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        filed = cast(list[int], d.pop("filed"))

        recovered = cast(list[int], d.pop("recovered"))

        remaining_capacity = d.pop("remaining_capacity")

        success = d.pop("success", UNSET)

        git_hub_issue_triage_response = cls(
            filed=filed,
            recovered=recovered,
            remaining_capacity=remaining_capacity,
            success=success,
        )

        git_hub_issue_triage_response.additional_properties = d
        return git_hub_issue_triage_response

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
