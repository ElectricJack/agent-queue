from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="GenerateReadmeResponse")


@_attrs_define
class GenerateReadmeResponse:
    """
    Attributes:
        project_id (str):
        readme_path (str | Unset):  Default: ''.
        committed (bool | Unset):  Default: False.
        pushed (bool | Unset):  Default: False.
        status (None | str | Unset):
        message (None | str | Unset):
    """

    project_id: str
    readme_path: str | Unset = ""
    committed: bool | Unset = False
    pushed: bool | Unset = False
    status: None | str | Unset = UNSET
    message: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        readme_path = self.readme_path

        committed = self.committed

        pushed = self.pushed

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

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
            }
        )
        if readme_path is not UNSET:
            field_dict["readme_path"] = readme_path
        if committed is not UNSET:
            field_dict["committed"] = committed
        if pushed is not UNSET:
            field_dict["pushed"] = pushed
        if status is not UNSET:
            field_dict["status"] = status
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        readme_path = d.pop("readme_path", UNSET)

        committed = d.pop("committed", UNSET)

        pushed = d.pop("pushed", UNSET)

        def _parse_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        generate_readme_response = cls(
            project_id=project_id,
            readme_path=readme_path,
            committed=committed,
            pushed=pushed,
            status=status,
            message=message,
        )

        generate_readme_response.additional_properties = d
        return generate_readme_response

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
