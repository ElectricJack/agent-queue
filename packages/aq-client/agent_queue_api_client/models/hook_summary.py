from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.hook_summary_trigger import HookSummaryTrigger


T = TypeVar("T", bound="HookSummary")


@_attrs_define
class HookSummary:
    """
    Attributes:
        id (str):
        project_id (str | Unset):  Default: ''.
        name (str | Unset):  Default: ''.
        enabled (bool | Unset):  Default: True.
        trigger (HookSummaryTrigger | Unset):
        cooldown_seconds (int | Unset):  Default: 0.
        prompt_template (str | Unset):  Default: ''.
    """

    id: str
    project_id: str | Unset = ""
    name: str | Unset = ""
    enabled: bool | Unset = True
    trigger: HookSummaryTrigger | Unset = UNSET
    cooldown_seconds: int | Unset = 0
    prompt_template: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        project_id = self.project_id

        name = self.name

        enabled = self.enabled

        trigger: dict[str, Any] | Unset = UNSET
        if not isinstance(self.trigger, Unset):
            trigger = self.trigger.to_dict()

        cooldown_seconds = self.cooldown_seconds

        prompt_template = self.prompt_template

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
            }
        )
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if name is not UNSET:
            field_dict["name"] = name
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if trigger is not UNSET:
            field_dict["trigger"] = trigger
        if cooldown_seconds is not UNSET:
            field_dict["cooldown_seconds"] = cooldown_seconds
        if prompt_template is not UNSET:
            field_dict["prompt_template"] = prompt_template

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.hook_summary_trigger import HookSummaryTrigger

        d = dict(src_dict)
        id = d.pop("id")

        project_id = d.pop("project_id", UNSET)

        name = d.pop("name", UNSET)

        enabled = d.pop("enabled", UNSET)

        _trigger = d.pop("trigger", UNSET)
        trigger: HookSummaryTrigger | Unset
        if isinstance(_trigger, Unset):
            trigger = UNSET
        else:
            trigger = HookSummaryTrigger.from_dict(_trigger)

        cooldown_seconds = d.pop("cooldown_seconds", UNSET)

        prompt_template = d.pop("prompt_template", UNSET)

        hook_summary = cls(
            id=id,
            project_id=project_id,
            name=name,
            enabled=enabled,
            trigger=trigger,
            cooldown_seconds=cooldown_seconds,
            prompt_template=prompt_template,
        )

        hook_summary.additional_properties = d
        return hook_summary

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
