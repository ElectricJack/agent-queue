from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreateAgentRequest")


@_attrs_define
class CreateAgentRequest:
    """
    Attributes:
        name (str):
        profile_id (str):
        harness (None | str | Unset):
        model (None | str | Unset):
        intelligence_class (None | str | Unset):
        enabled (bool | Unset):  Default: True.
    """

    name: str
    profile_id: str
    harness: None | str | Unset = UNSET
    model: None | str | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    enabled: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        name = self.name

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

        enabled = self.enabled

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "name": name,
                "profile_id": profile_id,
            }
        )
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
        name = d.pop("name")

        profile_id = d.pop("profile_id")

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

        enabled = d.pop("enabled", UNSET)

        create_agent_request = cls(
            name=name,
            profile_id=profile_id,
            harness=harness,
            model=model,
            intelligence_class=intelligence_class,
            enabled=enabled,
        )

        create_agent_request.additional_properties = d
        return create_agent_request

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
