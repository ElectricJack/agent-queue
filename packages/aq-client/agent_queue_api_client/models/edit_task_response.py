from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EditTaskResponse")


@_attrs_define
class EditTaskResponse:
    """
    Attributes:
        updated (str):
        fields (list[str]):
        old_status (None | str | Unset):
        new_status (None | str | Unset):
    """

    updated: str
    fields: list[str]
    old_status: None | str | Unset = UNSET
    new_status: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        updated = self.updated

        fields = self.fields

        old_status: None | str | Unset
        if isinstance(self.old_status, Unset):
            old_status = UNSET
        else:
            old_status = self.old_status

        new_status: None | str | Unset
        if isinstance(self.new_status, Unset):
            new_status = UNSET
        else:
            new_status = self.new_status

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "updated": updated,
                "fields": fields,
            }
        )
        if old_status is not UNSET:
            field_dict["old_status"] = old_status
        if new_status is not UNSET:
            field_dict["new_status"] = new_status

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        updated = d.pop("updated")

        fields = cast(list[str], d.pop("fields"))

        def _parse_old_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        old_status = _parse_old_status(d.pop("old_status", UNSET))

        def _parse_new_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        new_status = _parse_new_status(d.pop("new_status", UNSET))

        edit_task_response = cls(
            updated=updated,
            fields=fields,
            old_status=old_status,
            new_status=new_status,
        )

        edit_task_response.additional_properties = d
        return edit_task_response

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
