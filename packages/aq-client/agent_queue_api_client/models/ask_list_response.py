from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ask_source_record import AskSourceRecord


T = TypeVar("T", bound="AskListResponse")


@_attrs_define
class AskListResponse:
    """
    Attributes:
        identity_hash (str):
        items (list[AskSourceRecord]):
        count (int):
        snapshot_at (float):
        source_health (str):
        view (str | Unset):  Default: 'needs_you'.
        success (bool | Unset):  Default: True.
        next_cursor (None | str | Unset):
    """

    identity_hash: str
    items: list[AskSourceRecord]
    count: int
    snapshot_at: float
    source_health: str
    view: str | Unset = "needs_you"
    success: bool | Unset = True
    next_cursor: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        identity_hash = self.identity_hash

        items = []
        for items_item_data in self.items:
            items_item = items_item_data.to_dict()
            items.append(items_item)

        count = self.count

        snapshot_at = self.snapshot_at

        source_health = self.source_health

        view = self.view

        success = self.success

        next_cursor: None | str | Unset
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "identity_hash": identity_hash,
                "items": items,
                "count": count,
                "snapshot_at": snapshot_at,
                "source_health": source_health,
            }
        )
        if view is not UNSET:
            field_dict["view"] = view
        if success is not UNSET:
            field_dict["success"] = success
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ask_source_record import AskSourceRecord

        d = dict(src_dict)
        identity_hash = d.pop("identity_hash")

        items = []
        _items = d.pop("items")
        for items_item_data in _items:
            items_item = AskSourceRecord.from_dict(items_item_data)

            items.append(items_item)

        count = d.pop("count")

        snapshot_at = d.pop("snapshot_at")

        source_health = d.pop("source_health")

        view = d.pop("view", UNSET)

        success = d.pop("success", UNSET)

        def _parse_next_cursor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))

        ask_list_response = cls(
            identity_hash=identity_hash,
            items=items,
            count=count,
            snapshot_at=snapshot_at,
            source_health=source_health,
            view=view,
            success=success,
            next_cursor=next_cursor,
        )

        ask_list_response.additional_properties = d
        return ask_list_response

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
