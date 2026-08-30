from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EditAgentRequest")


@_attrs_define
class EditAgentRequest:
    """
    Attributes:
        agent_id (str):
        name (None | str | Unset):
        profile_id (None | str | Unset):
        harness (None | str | Unset):
        model (None | str | Unset):
        intelligence_class (None | str | Unset):
        enabled (bool | None | Unset):
    """

    agent_id: str
    name: None | str | Unset = UNSET
    profile_id: None | str | Unset = UNSET
    harness: None | str | Unset = UNSET
    model: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    enabled: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        agent_id = self.agent_id

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        profile_id: None | str | Unset
        if isinstance(self.profile_id, Unset):
            profile_id = UNSET
        else:
            profile_id = self.profile_id

        harness: None | str | Unset
        if isinstance(self.harness, Unset):
            harness = UNSET
        else:
            harness = self.harness

        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        enabled: bool | None | Unset
        if isinstance(self.enabled, Unset):
            enabled = UNSET
        else:
            enabled = self.enabled

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "agent_id": agent_id,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if profile_id is not UNSET:
            field_dict["profile_id"] = profile_id
        if harness is not UNSET:
            field_dict["harness"] = harness
        if model is not UNSET:
            field_dict["model"] = model
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if enabled is not UNSET:
            field_dict["enabled"] = enabled

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_id = d.pop("agent_id")

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_profile_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_id = _parse_profile_id(d.pop("profile_id", UNSET))

        def _parse_harness(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        harness = _parse_harness(d.pop("harness", UNSET))

        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_enabled(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        enabled = _parse_enabled(d.pop("enabled", UNSET))

        edit_agent_request = cls(
            agent_id=agent_id,
            name=name,
            profile_id=profile_id,
            harness=harness,
            model=model,
            intelligence_class=intelligence_class,
            enabled=enabled,
        )

        edit_agent_request.additional_properties = d
        return edit_agent_request

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
