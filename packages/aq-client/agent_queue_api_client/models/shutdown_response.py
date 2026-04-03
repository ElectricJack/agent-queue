from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ShutdownResponse")


@_attrs_define
class ShutdownResponse:
    """
    Attributes:
        status (str | Unset):  Default: 'shutting_down'.
        mode (str | Unset):  Default: 'graceful'.
        reason (str | Unset):  Default: ''.
        timestamp (str | Unset):  Default: ''.
        tasks_stopped (int | Unset):  Default: 0.
    """

    status: str | Unset = "shutting_down"
    mode: str | Unset = "graceful"
    reason: str | Unset = ""
    timestamp: str | Unset = ""
    tasks_stopped: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = self.status

        mode = self.mode

        reason = self.reason

        timestamp = self.timestamp

        tasks_stopped = self.tasks_stopped

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if status is not UNSET:
            field_dict["status"] = status
        if mode is not UNSET:
            field_dict["mode"] = mode
        if reason is not UNSET:
            field_dict["reason"] = reason
        if timestamp is not UNSET:
            field_dict["timestamp"] = timestamp
        if tasks_stopped is not UNSET:
            field_dict["tasks_stopped"] = tasks_stopped

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = d.pop("status", UNSET)

        mode = d.pop("mode", UNSET)

        reason = d.pop("reason", UNSET)

        timestamp = d.pop("timestamp", UNSET)

        tasks_stopped = d.pop("tasks_stopped", UNSET)

        shutdown_response = cls(
            status=status,
            mode=mode,
            reason=reason,
            timestamp=timestamp,
            tasks_stopped=tasks_stopped,
        )

        shutdown_response.additional_properties = d
        return shutdown_response

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
