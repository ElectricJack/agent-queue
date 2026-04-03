from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EditHookResponse")


@_attrs_define
class EditHookResponse:
    """
    Attributes:
        updated (str):
        fields (list[str] | Unset):
        warning (str | Unset):  Default: ''.
    """

    updated: str
    fields: list[str] | Unset = UNSET
    warning: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        updated = self.updated

        fields: list[str] | Unset = UNSET
        if not isinstance(self.fields, Unset):
            fields = self.fields

        warning = self.warning

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "updated": updated,
            }
        )
        if fields is not UNSET:
            field_dict["fields"] = fields
        if warning is not UNSET:
            field_dict["warning"] = warning

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        updated = d.pop("updated")

        fields = cast(list[str], d.pop("fields", UNSET))

        warning = d.pop("warning", UNSET)

        edit_hook_response = cls(
            updated=updated,
            fields=fields,
            warning=warning,
        )

        edit_hook_response.additional_properties = d
        return edit_hook_response

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
