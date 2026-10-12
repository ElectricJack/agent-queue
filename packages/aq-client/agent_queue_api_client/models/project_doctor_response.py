from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ProjectDoctorResponse")


@_attrs_define
class ProjectDoctorResponse:
    """
    Attributes:
        project_id (str):
        ready (bool):
        code (str):
        success (bool | Unset):  Default: True.
        recovery_command (None | str | Unset):
        message (None | str | Unset):
    """

    project_id: str
    ready: bool
    code: str
    success: bool | Unset = True
    recovery_command: None | str | Unset = UNSET
    message: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        ready = self.ready

        code = self.code

        success = self.success

        recovery_command: None | str | Unset
        if isinstance(self.recovery_command, Unset):
            recovery_command = UNSET
        else:
            recovery_command = self.recovery_command

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "ready": ready,
                "code": code,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if recovery_command is not UNSET:
            field_dict["recovery_command"] = recovery_command
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        ready = d.pop("ready")

        code = d.pop("code")

        success = d.pop("success", UNSET)

        def _parse_recovery_command(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        recovery_command = _parse_recovery_command(d.pop("recovery_command", UNSET))

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        project_doctor_response = cls(
            project_id=project_id,
            ready=ready,
            code=code,
            success=success,
            recovery_command=recovery_command,
            message=message,
        )

        project_doctor_response.additional_properties = d
        return project_doctor_response

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
