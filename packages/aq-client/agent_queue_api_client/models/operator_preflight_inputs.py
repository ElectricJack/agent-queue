from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OperatorPreflightInputs")


@_attrs_define
class OperatorPreflightInputs:
    """
    Attributes:
        branches (list[str] | Unset):
        runner_labels (list[str] | Unset):
        app_ids (list[int] | Unset):
    """

    branches: list[str] | Unset = UNSET
    runner_labels: list[str] | Unset = UNSET
    app_ids: list[int] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        branches: list[str] | Unset = UNSET
        if not isinstance(self.branches, Unset):
            branches = self.branches

        runner_labels: list[str] | Unset = UNSET
        if not isinstance(self.runner_labels, Unset):
            runner_labels = self.runner_labels

        app_ids: list[int] | Unset = UNSET
        if not isinstance(self.app_ids, Unset):
            app_ids = self.app_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if branches is not UNSET:
            field_dict["branches"] = branches
        if runner_labels is not UNSET:
            field_dict["runner_labels"] = runner_labels
        if app_ids is not UNSET:
            field_dict["app_ids"] = app_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        branches = cast(list[str], d.pop("branches", UNSET))

        runner_labels = cast(list[str], d.pop("runner_labels", UNSET))

        app_ids = cast(list[int], d.pop("app_ids", UNSET))

        operator_preflight_inputs = cls(
            branches=branches,
            runner_labels=runner_labels,
            app_ids=app_ids,
        )

        operator_preflight_inputs.additional_properties = d
        return operator_preflight_inputs

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
