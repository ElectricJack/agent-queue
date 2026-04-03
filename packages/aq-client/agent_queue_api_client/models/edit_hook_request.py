from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EditHookRequest")


@_attrs_define
class EditHookRequest:
    """
    Attributes:
        hook_id (str): Hook ID to edit
        name (None | str | Unset): New hook name
        enabled (bool | None | Unset): Enable/disable
        trigger (None | str | Unset): New trigger type
        prompt_template (None | str | Unset): New prompt template
    """

    hook_id: str
    name: None | str | Unset = UNSET
    enabled: bool | None | Unset = UNSET
    trigger: None | str | Unset = UNSET
    prompt_template: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        hook_id = self.hook_id

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        enabled: bool | None | Unset
        if isinstance(self.enabled, Unset):
            enabled = UNSET
        else:
            enabled = self.enabled

        trigger: None | str | Unset
        if isinstance(self.trigger, Unset):
            trigger = UNSET
        else:
            trigger = self.trigger

        prompt_template: None | str | Unset
        if isinstance(self.prompt_template, Unset):
            prompt_template = UNSET
        else:
            prompt_template = self.prompt_template

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "hook_id": hook_id,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if trigger is not UNSET:
            field_dict["trigger"] = trigger
        if prompt_template is not UNSET:
            field_dict["prompt_template"] = prompt_template

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        hook_id = d.pop("hook_id")

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_enabled(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        enabled = _parse_enabled(d.pop("enabled", UNSET))

        def _parse_trigger(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        trigger = _parse_trigger(d.pop("trigger", UNSET))

        def _parse_prompt_template(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        prompt_template = _parse_prompt_template(d.pop("prompt_template", UNSET))

        edit_hook_request = cls(
            hook_id=hook_id,
            name=name,
            enabled=enabled,
            trigger=trigger,
            prompt_template=prompt_template,
        )

        edit_hook_request.additional_properties = d
        return edit_hook_request

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
