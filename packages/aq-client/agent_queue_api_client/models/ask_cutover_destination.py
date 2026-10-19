from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="AskCutoverDestination")


@_attrs_define
class AskCutoverDestination:
    """
    Attributes:
        destination (str):
        mode (str):
        generation (int):
        migrated (bool):
        first_switch_utc (None | str):
        compatibility_deadline (float | None):
        compatibility_deadline_utc (None | str):
    """

    destination: str
    mode: str
    generation: int
    migrated: bool
    first_switch_utc: None | str
    compatibility_deadline: float | None
    compatibility_deadline_utc: None | str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        destination = self.destination

        mode = self.mode

        generation = self.generation

        migrated = self.migrated

        first_switch_utc: None | str
        first_switch_utc = self.first_switch_utc

        compatibility_deadline: float | None
        compatibility_deadline = self.compatibility_deadline

        compatibility_deadline_utc: None | str
        compatibility_deadline_utc = self.compatibility_deadline_utc

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "destination": destination,
                "mode": mode,
                "generation": generation,
                "migrated": migrated,
                "first_switch_utc": first_switch_utc,
                "compatibility_deadline": compatibility_deadline,
                "compatibility_deadline_utc": compatibility_deadline_utc,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        destination = d.pop("destination")

        mode = d.pop("mode")

        generation = d.pop("generation")

        migrated = d.pop("migrated")

        def _parse_first_switch_utc(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        first_switch_utc = _parse_first_switch_utc(d.pop("first_switch_utc"))

        def _parse_compatibility_deadline(data: object) -> float | None:
            if data is None:
                return data
            return cast(float | None, data)

        compatibility_deadline = _parse_compatibility_deadline(d.pop("compatibility_deadline"))

        def _parse_compatibility_deadline_utc(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        compatibility_deadline_utc = _parse_compatibility_deadline_utc(d.pop("compatibility_deadline_utc"))

        ask_cutover_destination = cls(
            destination=destination,
            mode=mode,
            generation=generation,
            migrated=migrated,
            first_switch_utc=first_switch_utc,
            compatibility_deadline=compatibility_deadline,
            compatibility_deadline_utc=compatibility_deadline_utc,
        )

        ask_cutover_destination.additional_properties = d
        return ask_cutover_destination

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
