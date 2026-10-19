from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="AskShowRequest")


@_attrs_define
class AskShowRequest:
    """
    Attributes:
        ask_id (str):
        alias_kind (None | str | Unset):
        input_id (None | str | Unset):
    """

    ask_id: str
    alias_kind: None | str | Unset = UNSET
    input_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        ask_id = self.ask_id

        alias_kind: None | str | Unset
        if isinstance(self.alias_kind, Unset):
            alias_kind = UNSET
        else:
            alias_kind = self.alias_kind

        input_id: None | str | Unset
        if isinstance(self.input_id, Unset):
            input_id = UNSET
        else:
            input_id = self.input_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "ask_id": ask_id,
            }
        )
        if alias_kind is not UNSET:
            field_dict["alias_kind"] = alias_kind
        if input_id is not UNSET:
            field_dict["input_id"] = input_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        ask_id = d.pop("ask_id")

        def _parse_alias_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        alias_kind = _parse_alias_kind(d.pop("alias_kind", UNSET))

        def _parse_input_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        input_id = _parse_input_id(d.pop("input_id", UNSET))

        ask_show_request = cls(
            ask_id=ask_id,
            alias_kind=alias_kind,
            input_id=input_id,
        )

        ask_show_request.additional_properties = d
        return ask_show_request

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
