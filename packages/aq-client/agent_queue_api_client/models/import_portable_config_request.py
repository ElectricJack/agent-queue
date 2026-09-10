from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ImportPortableConfigRequest")


@_attrs_define
class ImportPortableConfigRequest:
    """
    Attributes:
        source (str): Input .aqbundle path
        conflict (str | Unset):  Default: 'keep'.
        dry_run (bool | Unset):  Default: False.
    """

    source: str
    conflict: str | Unset = "keep"
    dry_run: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        source = self.source

        conflict = self.conflict

        dry_run = self.dry_run

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "source": source,
            }
        )
        if conflict is not UNSET:
            field_dict["conflict"] = conflict
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        source = d.pop("source")

        conflict = d.pop("conflict", UNSET)

        dry_run = d.pop("dry_run", UNSET)

        import_portable_config_request = cls(
            source=source,
            conflict=conflict,
            dry_run=dry_run,
        )

        import_portable_config_request.additional_properties = d
        return import_portable_config_request

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
