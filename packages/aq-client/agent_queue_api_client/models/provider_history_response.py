from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_transition import ProviderTransition


T = TypeVar("T", bound="ProviderHistoryResponse")


@_attrs_define
class ProviderHistoryResponse:
    """
    Attributes:
        provider (str):
        success (bool | Unset):  Default: True.
        transitions (list[ProviderTransition] | Unset):
    """

    provider: str
    success: bool | Unset = True
    transitions: list[ProviderTransition] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        provider = self.provider

        success = self.success

        transitions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.transitions, Unset):
            transitions = []
            for transitions_item_data in self.transitions:
                transitions_item = transitions_item_data.to_dict()
                transitions.append(transitions_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "provider": provider,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if transitions is not UNSET:
            field_dict["transitions"] = transitions

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_transition import ProviderTransition

        d = dict(src_dict)
        provider = d.pop("provider")

        success = d.pop("success", UNSET)

        _transitions = d.pop("transitions", UNSET)
        transitions: list[ProviderTransition] | Unset = UNSET
        if _transitions is not UNSET:
            transitions = []
            for transitions_item_data in _transitions:
                transitions_item = ProviderTransition.from_dict(transitions_item_data)

                transitions.append(transitions_item)

        provider_history_response = cls(
            provider=provider,
            success=success,
            transitions=transitions,
        )

        provider_history_response.additional_properties = d
        return provider_history_response

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
