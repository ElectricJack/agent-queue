from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateProfileResponse")


@_attrs_define
class CreateProfileResponse:
    """
    Attributes:
        created (str):
        name (str):
        warnings (list[str] | None | Unset):
    """

    created: str
    name: str
    warnings: list[str] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        name = self.name

        warnings: list[str] | None | Unset
        if isinstance(self.warnings, Unset):
            warnings = UNSET
        elif isinstance(self.warnings, list):
            warnings = self.warnings

        else:
            warnings = self.warnings

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
                "name": name,
            }
        )
        if warnings is not UNSET:
            field_dict["warnings"] = warnings

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created = d.pop("created")

        name = d.pop("name")

        def _parse_warnings(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                warnings_type_0 = cast(list[str], data)

                return warnings_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        warnings = _parse_warnings(d.pop("warnings", UNSET))

        create_profile_response = cls(
            created=created,
            name=name,
            warnings=warnings,
        )

        create_profile_response.additional_properties = d
        return create_profile_response

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
