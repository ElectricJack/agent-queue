from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="UpdateAndRestartResponse")


@_attrs_define
class UpdateAndRestartResponse:
    """
    Attributes:
        status (str | Unset):  Default: 'updating'.
        message (str | Unset):  Default: ''.
        pull_output (str | Unset):  Default: ''.
        reason (str | Unset):  Default: ''.
    """

    status: str | Unset = "updating"
    message: str | Unset = ""
    pull_output: str | Unset = ""
    reason: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = self.status

        message = self.message

        pull_output = self.pull_output

        reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if status is not UNSET:
            field_dict["status"] = status
        if message is not UNSET:
            field_dict["message"] = message
        if pull_output is not UNSET:
            field_dict["pull_output"] = pull_output
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = d.pop("status", UNSET)

        message = d.pop("message", UNSET)

        pull_output = d.pop("pull_output", UNSET)

        reason = d.pop("reason", UNSET)

        update_and_restart_response = cls(
            status=status,
            message=message,
            pull_output=pull_output,
            reason=reason,
        )

        update_and_restart_response.additional_properties = d
        return update_and_restart_response

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
