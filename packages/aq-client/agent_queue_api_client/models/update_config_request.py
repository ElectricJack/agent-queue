from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.update_config_request_data_type_0 import UpdateConfigRequestDataType0


T = TypeVar("T", bound="UpdateConfigRequest")


@_attrs_define
class UpdateConfigRequest:
    """
    Attributes:
        section (str): Top-level section to replace (e.g. 'scheduling').
        data (bool | float | list[Any] | None | str | UpdateConfigRequestDataType0): New value for the section. null to
            delete.
        dry_run (bool | Unset): Validate but don't persist. Default: False.
    """

    section: str
    data: bool | float | list[Any] | None | str | UpdateConfigRequestDataType0
    dry_run: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.update_config_request_data_type_0 import UpdateConfigRequestDataType0

        section = self.section

        data: bool | dict[str, Any] | float | list[Any] | None | str
        if isinstance(self.data, UpdateConfigRequestDataType0):
            data = self.data.to_dict()
        elif isinstance(self.data, list):
            data = self.data

        else:
            data = self.data

        dry_run = self.dry_run

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "section": section,
                "data": data,
            }
        )
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.update_config_request_data_type_0 import UpdateConfigRequestDataType0

        d = dict(src_dict)
        section = d.pop("section")

        def _parse_data(data: object) -> bool | float | list[Any] | None | str | UpdateConfigRequestDataType0:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                data_type_0 = UpdateConfigRequestDataType0.from_dict(data)

                return data_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, list):
                    raise TypeError()
                data_type_1 = cast(list[Any], data)

                return data_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(bool | float | list[Any] | None | str | UpdateConfigRequestDataType0, data)

        data = _parse_data(d.pop("data"))

        dry_run = d.pop("dry_run", UNSET)

        update_config_request = cls(
            section=section,
            data=data,
            dry_run=dry_run,
        )

        update_config_request.additional_properties = d
        return update_config_request

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
