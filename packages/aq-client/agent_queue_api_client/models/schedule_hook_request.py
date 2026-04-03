from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.schedule_hook_request_llm_config_type_0 import ScheduleHookRequestLlmConfigType0


T = TypeVar("T", bound="ScheduleHookRequest")


@_attrs_define
class ScheduleHookRequest:
    """
    Attributes:
        project_id (str): Project ID
        prompt_template (str): Prompt template to execute when the scheduled time arrives
        name (str | Unset): Descriptive name for the scheduled hook (optional, used as ID slug) Default: 'scheduled-
            hook'.
        fire_at (float | None | Unset): When to fire: epoch timestamp (number) or ISO-8601 datetime string. Mutually
            exclusive with 'delay'.
        delay (None | str | Unset): Delay before firing: e.g. '30s', '5m', '2h', '1d', '2h30m'. Mutually exclusive with
            'fire_at'.
        context_steps (list[Any] | None | Unset): Optional context-gathering steps
        llm_config (None | ScheduleHookRequestLlmConfigType0 | Unset): Optional LLM config override: {provider, model,
            base_url}
    """

    project_id: str
    prompt_template: str
    name: str | Unset = "scheduled-hook"
    fire_at: float | None | Unset = UNSET
    delay: None | str | Unset = UNSET
    context_steps: list[Any] | None | Unset = UNSET
    llm_config: None | ScheduleHookRequestLlmConfigType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.schedule_hook_request_llm_config_type_0 import ScheduleHookRequestLlmConfigType0

        project_id = self.project_id

        prompt_template = self.prompt_template

        name = self.name

        fire_at: float | None | Unset
        if isinstance(self.fire_at, Unset):
            fire_at = UNSET
        else:
            fire_at = self.fire_at

        delay: None | str | Unset
        if isinstance(self.delay, Unset):
            delay = UNSET
        else:
            delay = self.delay

        context_steps: list[Any] | None | Unset
        if isinstance(self.context_steps, Unset):
            context_steps = UNSET
        elif isinstance(self.context_steps, list):
            context_steps = self.context_steps

        else:
            context_steps = self.context_steps

        llm_config: dict[str, Any] | None | Unset
        if isinstance(self.llm_config, Unset):
            llm_config = UNSET
        elif isinstance(self.llm_config, ScheduleHookRequestLlmConfigType0):
            llm_config = self.llm_config.to_dict()
        else:
            llm_config = self.llm_config

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "prompt_template": prompt_template,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if fire_at is not UNSET:
            field_dict["fire_at"] = fire_at
        if delay is not UNSET:
            field_dict["delay"] = delay
        if context_steps is not UNSET:
            field_dict["context_steps"] = context_steps
        if llm_config is not UNSET:
            field_dict["llm_config"] = llm_config

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.schedule_hook_request_llm_config_type_0 import ScheduleHookRequestLlmConfigType0

        d = dict(src_dict)
        project_id = d.pop("project_id")

        prompt_template = d.pop("prompt_template")

        name = d.pop("name", UNSET)

        def _parse_fire_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        fire_at = _parse_fire_at(d.pop("fire_at", UNSET))

        def _parse_delay(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        delay = _parse_delay(d.pop("delay", UNSET))

        def _parse_context_steps(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                context_steps_type_0 = cast(list[Any], data)

                return context_steps_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        context_steps = _parse_context_steps(d.pop("context_steps", UNSET))

        def _parse_llm_config(data: object) -> None | ScheduleHookRequestLlmConfigType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                llm_config_type_0 = ScheduleHookRequestLlmConfigType0.from_dict(data)

                return llm_config_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ScheduleHookRequestLlmConfigType0 | Unset, data)

        llm_config = _parse_llm_config(d.pop("llm_config", UNSET))

        schedule_hook_request = cls(
            project_id=project_id,
            prompt_template=prompt_template,
            name=name,
            fire_at=fire_at,
            delay=delay,
            context_steps=context_steps,
            llm_config=llm_config,
        )

        schedule_hook_request.additional_properties = d
        return schedule_hook_request

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
