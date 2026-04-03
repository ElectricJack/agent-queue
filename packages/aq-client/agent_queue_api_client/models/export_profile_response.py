from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ExportProfileResponse")


@_attrs_define
class ExportProfileResponse:
    """
    Attributes:
        yaml (str | Unset):  Default: ''.
        gist_url (None | str | Unset):
        gist_error (None | str | Unset):
    """

    yaml: str | Unset = ""
    gist_url: None | str | Unset = UNSET
    gist_error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        yaml = self.yaml

        gist_url: None | str | Unset
        if isinstance(self.gist_url, Unset):
            gist_url = UNSET
        else:
            gist_url = self.gist_url

        gist_error: None | str | Unset
        if isinstance(self.gist_error, Unset):
            gist_error = UNSET
        else:
            gist_error = self.gist_error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if yaml is not UNSET:
            field_dict["yaml"] = yaml
        if gist_url is not UNSET:
            field_dict["gist_url"] = gist_url
        if gist_error is not UNSET:
            field_dict["gist_error"] = gist_error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        yaml = d.pop("yaml", UNSET)

        def _parse_gist_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        gist_url = _parse_gist_url(d.pop("gist_url", UNSET))

        def _parse_gist_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        gist_error = _parse_gist_error(d.pop("gist_error", UNSET))

        export_profile_response = cls(
            yaml=yaml,
            gist_url=gist_url,
            gist_error=gist_error,
        )

        export_profile_response.additional_properties = d
        return export_profile_response

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
