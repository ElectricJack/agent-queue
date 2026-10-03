from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="KnowledgeGenerationTickResponse")


@_attrs_define
class KnowledgeGenerationTickResponse:
    """One bounded tick: how much was retained and the terminal state per job.

    ``states`` carries one terminal state string per job the tick ran
    (``succeeded`` / ``retry`` / ``quarantined`` / ``cancelled``); it is empty
    when no due job was claimed.

        Attributes:
            success (bool | Unset):  Default: True.
            captured (int | None | Unset):
            states (list[str] | Unset):
    """

    success: bool | Unset = True
    captured: int | None | Unset = UNSET
    states: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        captured: int | None | Unset
        if isinstance(self.captured, Unset):
            captured = UNSET
        else:
            captured = self.captured

        states: list[str] | Unset = UNSET
        if not isinstance(self.states, Unset):
            states = self.states

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if captured is not UNSET:
            field_dict["captured"] = captured
        if states is not UNSET:
            field_dict["states"] = states

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_captured(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        captured = _parse_captured(d.pop("captured", UNSET))

        states = cast(list[str], d.pop("states", UNSET))

        knowledge_generation_tick_response = cls(
            success=success,
            captured=captured,
            states=states,
        )

        knowledge_generation_tick_response.additional_properties = d
        return knowledge_generation_tick_response

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
