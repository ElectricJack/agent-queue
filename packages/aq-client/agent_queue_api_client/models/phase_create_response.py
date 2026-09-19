from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.phase_ref import PhaseRef


T = TypeVar("T", bound="PhaseCreateResponse")


@_attrs_define
class PhaseCreateResponse:
    """
    Attributes:
        phase (PhaseRef): The phase ``phase_create`` just wrote.
    """

    phase: PhaseRef
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        phase = self.phase.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "phase": phase,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.phase_ref import PhaseRef

        d = dict(src_dict)
        phase = PhaseRef.from_dict(d.pop("phase"))

        phase_create_response = cls(
            phase=phase,
        )

        phase_create_response.additional_properties = d
        return phase_create_response

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
