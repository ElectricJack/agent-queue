from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ImportProfileResponse")


@_attrs_define
class ImportProfileResponse:
    """
    Attributes:
        imported (bool | Unset):  Default: False.
        name (str | Unset):  Default: ''.
        id (str | Unset):  Default: ''.
        installed (list[str] | None | Unset):
        already_present (list[str] | None | Unset):
        manual (list[str] | None | Unset):
        ready (bool | Unset):  Default: False.
    """

    imported: bool | Unset = False
    name: str | Unset = ""
    id: str | Unset = ""
    installed: list[str] | None | Unset = UNSET
    already_present: list[str] | None | Unset = UNSET
    manual: list[str] | None | Unset = UNSET
    ready: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        imported = self.imported

        name = self.name

        id = self.id

        installed: list[str] | None | Unset
        if isinstance(self.installed, Unset):
            installed = UNSET
        elif isinstance(self.installed, list):
            installed = self.installed

        else:
            installed = self.installed

        already_present: list[str] | None | Unset
        if isinstance(self.already_present, Unset):
            already_present = UNSET
        elif isinstance(self.already_present, list):
            already_present = self.already_present

        else:
            already_present = self.already_present

        manual: list[str] | None | Unset
        if isinstance(self.manual, Unset):
            manual = UNSET
        elif isinstance(self.manual, list):
            manual = self.manual

        else:
            manual = self.manual

        ready = self.ready

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if imported is not UNSET:
            field_dict["imported"] = imported
        if name is not UNSET:
            field_dict["name"] = name
        if id is not UNSET:
            field_dict["id"] = id
        if installed is not UNSET:
            field_dict["installed"] = installed
        if already_present is not UNSET:
            field_dict["already_present"] = already_present
        if manual is not UNSET:
            field_dict["manual"] = manual
        if ready is not UNSET:
            field_dict["ready"] = ready

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        imported = d.pop("imported", UNSET)

        name = d.pop("name", UNSET)

        id = d.pop("id", UNSET)

        def _parse_installed(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                installed_type_0 = cast(list[str], data)

                return installed_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        installed = _parse_installed(d.pop("installed", UNSET))

        def _parse_already_present(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                already_present_type_0 = cast(list[str], data)

                return already_present_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        already_present = _parse_already_present(d.pop("already_present", UNSET))

        def _parse_manual(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                manual_type_0 = cast(list[str], data)

                return manual_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        manual = _parse_manual(d.pop("manual", UNSET))

        ready = d.pop("ready", UNSET)

        import_profile_response = cls(
            imported=imported,
            name=name,
            id=id,
            installed=installed,
            already_present=already_present,
            manual=manual,
            ready=ready,
        )

        import_profile_response.additional_properties = d
        return import_profile_response

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
