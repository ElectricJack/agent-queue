from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.provider_allocation_ceiling import ProviderAllocationCeiling


T = TypeVar("T", bound="ProviderAllocationCeilingChange")


@_attrs_define
class ProviderAllocationCeilingChange:
    """The provider-wide configured ceiling before and after.

    Attributes:
        before (ProviderAllocationCeiling): The provider-wide configured ceiling over its enabled pool profiles.

            ``max_active`` is ``None`` (and ``unbounded`` true) when any of them is
            unbounded.  Bounds stay per profile; this total is for reading only.
        after (ProviderAllocationCeiling): The provider-wide configured ceiling over its enabled pool profiles.

            ``max_active`` is ``None`` (and ``unbounded`` true) when any of them is
            unbounded.  Bounds stay per profile; this total is for reading only.
    """

    before: ProviderAllocationCeiling
    after: ProviderAllocationCeiling
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        before = self.before.to_dict()

        after = self.after.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "before": before,
                "after": after,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_ceiling import ProviderAllocationCeiling

        d = dict(src_dict)
        before = ProviderAllocationCeiling.from_dict(d.pop("before"))

        after = ProviderAllocationCeiling.from_dict(d.pop("after"))

        provider_allocation_ceiling_change = cls(
            before=before,
            after=after,
        )

        provider_allocation_ceiling_change.additional_properties = d
        return provider_allocation_ceiling_change

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
