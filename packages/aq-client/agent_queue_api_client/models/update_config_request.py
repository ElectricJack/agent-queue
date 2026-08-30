from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.update_config_request_data import UpdateConfigRequestData


T = TypeVar("T", bound="UpdateConfigRequest")


@_attrs_define
class UpdateConfigRequest:
    """
    Attributes:
        section (str): Top-level section to replace (e.g. 'scheduling').
        data (UpdateConfigRequestData): New value for the section. null to delete.
        dry_run (bool | Unset): Validate but don't persist. Default: False.
    """

    section: str
    data: UpdateConfigRequestData
    dry_run: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        section = self.section

        data = self.data.to_dict()

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
        from ..models.update_config_request_data import UpdateConfigRequestData

        d = dict(src_dict)
        section = d.pop("section")

        data = UpdateConfigRequestData.from_dict(d.pop("data"))

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
