from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.list_scheduled_response_scheduled_hooks_item import ListScheduledResponseScheduledHooksItem


T = TypeVar("T", bound="ListScheduledResponse")


@_attrs_define
class ListScheduledResponse:
    """
    Attributes:
        scheduled_hooks (list[ListScheduledResponseScheduledHooksItem] | Unset):
        count (int | Unset):  Default: 0.
    """

    scheduled_hooks: list[ListScheduledResponseScheduledHooksItem] | Unset = UNSET
    count: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        scheduled_hooks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.scheduled_hooks, Unset):
            scheduled_hooks = []
            for scheduled_hooks_item_data in self.scheduled_hooks:
                scheduled_hooks_item = scheduled_hooks_item_data.to_dict()
                scheduled_hooks.append(scheduled_hooks_item)

        count = self.count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if scheduled_hooks is not UNSET:
            field_dict["scheduled_hooks"] = scheduled_hooks
        if count is not UNSET:
            field_dict["count"] = count

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.list_scheduled_response_scheduled_hooks_item import ListScheduledResponseScheduledHooksItem

        d = dict(src_dict)
        _scheduled_hooks = d.pop("scheduled_hooks", UNSET)
        scheduled_hooks: list[ListScheduledResponseScheduledHooksItem] | Unset = UNSET
        if _scheduled_hooks is not UNSET:
            scheduled_hooks = []
            for scheduled_hooks_item_data in _scheduled_hooks:
                scheduled_hooks_item = ListScheduledResponseScheduledHooksItem.from_dict(scheduled_hooks_item_data)

                scheduled_hooks.append(scheduled_hooks_item)

        count = d.pop("count", UNSET)

        list_scheduled_response = cls(
            scheduled_hooks=scheduled_hooks,
            count=count,
        )

        list_scheduled_response.additional_properties = d
        return list_scheduled_response

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
