from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MessageInboxRequest")


@_attrs_define
class MessageInboxRequest:
    """
    Attributes:
        to_kind (str): Recipient kind
        to_id (str): Recipient id
        inject (bool | Unset): Mark the returned messages delivered Default: False.
        limit (int | None | Unset): Max rows (default 50, or max_inject_per_prompt when injecting)
    """

    to_kind: str
    to_id: str
    inject: bool | Unset = False
    limit: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        to_kind = self.to_kind

        to_id = self.to_id

        inject = self.inject

        limit: int | None | Unset
        if isinstance(self.limit, Unset):
            limit = UNSET
        else:
            limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "to_kind": to_kind,
                "to_id": to_id,
            }
        )
        if inject is not UNSET:
            field_dict["inject"] = inject
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        to_kind = d.pop("to_kind")

        to_id = d.pop("to_id")

        inject = d.pop("inject", UNSET)

        def _parse_limit(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        limit = _parse_limit(d.pop("limit", UNSET))

        message_inbox_request = cls(
            to_kind=to_kind,
            to_id=to_id,
            inject=inject,
            limit=limit,
        )

        message_inbox_request.additional_properties = d
        return message_inbox_request

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
