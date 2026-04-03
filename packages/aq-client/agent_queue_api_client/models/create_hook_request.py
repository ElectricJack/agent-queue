from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateHookRequest")


@_attrs_define
class CreateHookRequest:
    """
    Attributes:
        name (str): Hook name
        trigger (str): Trigger type
        prompt_template (str): Prompt template
        project_id (None | str | Unset): Project ID
        cooldown_seconds (int | None | Unset): Cooldown between fires (seconds)
    """

    name: str
    trigger: str
    prompt_template: str
    project_id: None | str | Unset = UNSET
    cooldown_seconds: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

        trigger = self.trigger

        prompt_template = self.prompt_template

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        cooldown_seconds: int | None | Unset
        if isinstance(self.cooldown_seconds, Unset):
            cooldown_seconds = UNSET
        else:
            cooldown_seconds = self.cooldown_seconds

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "trigger": trigger,
                "prompt_template": prompt_template,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if cooldown_seconds is not UNSET:
            field_dict["cooldown_seconds"] = cooldown_seconds

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        trigger = d.pop("trigger")

        prompt_template = d.pop("prompt_template")

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_cooldown_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        cooldown_seconds = _parse_cooldown_seconds(d.pop("cooldown_seconds", UNSET))

        create_hook_request = cls(
            name=name,
            trigger=trigger,
            prompt_template=prompt_template,
            project_id=project_id,
            cooldown_seconds=cooldown_seconds,
        )

        create_hook_request.additional_properties = d
        return create_hook_request

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
