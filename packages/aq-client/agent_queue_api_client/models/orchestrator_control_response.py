from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="OrchestratorControlResponse")


@_attrs_define
class OrchestratorControlResponse:
    """
    Attributes:
        status (str | Unset):  Default: ''.
        message (None | str | Unset):
        running_tasks (int | None | Unset):
    """

    status: str | Unset = ""
    message: None | str | Unset = UNSET
    running_tasks: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        status = self.status

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        running_tasks: int | None | Unset
        if isinstance(self.running_tasks, Unset):
            running_tasks = UNSET
        else:
            running_tasks = self.running_tasks

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if status is not UNSET:
            field_dict["status"] = status
        if message is not UNSET:
            field_dict["message"] = message
        if running_tasks is not UNSET:
            field_dict["running_tasks"] = running_tasks

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = d.pop("status", UNSET)

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        def _parse_running_tasks(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        running_tasks = _parse_running_tasks(d.pop("running_tasks", UNSET))

        orchestrator_control_response = cls(
            status=status,
            message=message,
            running_tasks=running_tasks,
        )

        orchestrator_control_response.additional_properties = d
        return orchestrator_control_response

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
