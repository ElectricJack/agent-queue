from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitDeadline")


@_attrs_define
class WaitDeadline:
    """
    Attributes:
        type_ (str):
        due_at (float):
        generation (int | None | Unset):
        fired_at (float | None | Unset):
        cancelled_at (float | None | Unset):
    """

    type_: str
    due_at: float
    generation: int | None | Unset = UNSET
    fired_at: float | None | Unset = UNSET
    cancelled_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        type_ = self.type_

        due_at = self.due_at

        generation: int | None | Unset
        if isinstance(self.generation, Unset):
            generation = UNSET
        else:
            generation = self.generation

        fired_at: float | None | Unset
        if isinstance(self.fired_at, Unset):
            fired_at = UNSET
        else:
            fired_at = self.fired_at

        cancelled_at: float | None | Unset
        if isinstance(self.cancelled_at, Unset):
            cancelled_at = UNSET
        else:
            cancelled_at = self.cancelled_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "type": type_,
                "due_at": due_at,
            }
        )
        if generation is not UNSET:
            field_dict["generation"] = generation
        if fired_at is not UNSET:
            field_dict["fired_at"] = fired_at
        if cancelled_at is not UNSET:
            field_dict["cancelled_at"] = cancelled_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        type_ = d.pop("type")

        due_at = d.pop("due_at")

        def _parse_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        generation = _parse_generation(d.pop("generation", UNSET))

        def _parse_fired_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        fired_at = _parse_fired_at(d.pop("fired_at", UNSET))

        def _parse_cancelled_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        cancelled_at = _parse_cancelled_at(d.pop("cancelled_at", UNSET))

        wait_deadline = cls(
            type_=type_,
            due_at=due_at,
            generation=generation,
            fired_at=fired_at,
            cancelled_at=cancelled_at,
        )

        wait_deadline.additional_properties = d
        return wait_deadline

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
