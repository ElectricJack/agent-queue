from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.hook_schedules_response_hooks_item import HookSchedulesResponseHooksItem


T = TypeVar("T", bound="HookSchedulesResponse")


@_attrs_define
class HookSchedulesResponse:
    """
    Attributes:
        hooks (list[HookSchedulesResponseHooksItem] | Unset):
        message (None | str | Unset):
    """

    hooks: list[HookSchedulesResponseHooksItem] | Unset = UNSET
    message: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        hooks: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.hooks, Unset):
            hooks = []
            for hooks_item_data in self.hooks:
                hooks_item = hooks_item_data.to_dict()
                hooks.append(hooks_item)

        message: None | str | Unset
        if isinstance(self.message, Unset):
            message = UNSET
        else:
            message = self.message

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if hooks is not UNSET:
            field_dict["hooks"] = hooks
        if message is not UNSET:
            field_dict["message"] = message

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.hook_schedules_response_hooks_item import HookSchedulesResponseHooksItem

        d = dict(src_dict)
        _hooks = d.pop("hooks", UNSET)
        hooks: list[HookSchedulesResponseHooksItem] | Unset = UNSET
        if _hooks is not UNSET:
            hooks = []
            for hooks_item_data in _hooks:
                hooks_item = HookSchedulesResponseHooksItem.from_dict(hooks_item_data)

                hooks.append(hooks_item)

        def _parse_message(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        message = _parse_message(d.pop("message", UNSET))

        hook_schedules_response = cls(
            hooks=hooks,
            message=message,
        )

        hook_schedules_response.additional_properties = d
        return hook_schedules_response

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
