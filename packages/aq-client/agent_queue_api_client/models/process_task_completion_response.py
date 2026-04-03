from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProcessTaskCompletionResponse")


@_attrs_define
class ProcessTaskCompletionResponse:
    """
    Attributes:
        plan_found (bool | Unset):  Default: False.
        reason (None | str | Unset):
        plan_file (None | str | Unset):
        archived_path (None | str | Unset):
    """

    plan_found: bool | Unset = False
    reason: None | str | Unset = UNSET
    plan_file: None | str | Unset = UNSET
    archived_path: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        plan_found = self.plan_found

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        plan_file: None | str | Unset
        if isinstance(self.plan_file, Unset):
            plan_file = UNSET
        else:
            plan_file = self.plan_file

        archived_path: None | str | Unset
        if isinstance(self.archived_path, Unset):
            archived_path = UNSET
        else:
            archived_path = self.archived_path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if plan_found is not UNSET:
            field_dict["plan_found"] = plan_found
        if reason is not UNSET:
            field_dict["reason"] = reason
        if plan_file is not UNSET:
            field_dict["plan_file"] = plan_file
        if archived_path is not UNSET:
            field_dict["archived_path"] = archived_path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        plan_found = d.pop("plan_found", UNSET)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_plan_file(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        plan_file = _parse_plan_file(d.pop("plan_file", UNSET))

        def _parse_archived_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        archived_path = _parse_archived_path(d.pop("archived_path", UNSET))

        process_task_completion_response = cls(
            plan_found=plan_found,
            reason=reason,
            plan_file=plan_file,
            archived_path=archived_path,
        )

        process_task_completion_response.additional_properties = d
        return process_task_completion_response

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
