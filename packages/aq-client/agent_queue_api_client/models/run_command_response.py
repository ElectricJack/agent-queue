from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="RunCommandResponse")


@_attrs_define
class RunCommandResponse:
    """
    Attributes:
        returncode (int | Unset):  Default: 0.
        stdout (str | Unset):  Default: ''.
        stderr (str | Unset):  Default: ''.
    """

    returncode: int | Unset = 0
    stdout: str | Unset = ""
    stderr: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        returncode = self.returncode

        stdout = self.stdout

        stderr = self.stderr

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if returncode is not UNSET:
            field_dict["returncode"] = returncode
        if stdout is not UNSET:
            field_dict["stdout"] = stdout
        if stderr is not UNSET:
            field_dict["stderr"] = stderr

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        returncode = d.pop("returncode", UNSET)

        stdout = d.pop("stdout", UNSET)

        stderr = d.pop("stderr", UNSET)

        run_command_response = cls(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

        run_command_response.additional_properties = d
        return run_command_response

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
