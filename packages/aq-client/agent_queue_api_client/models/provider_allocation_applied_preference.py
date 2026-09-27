from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_placement import ProviderAllocationPlacement


T = TypeVar("T", bound="ProviderAllocationAppliedPreference")


@_attrs_define
class ProviderAllocationAppliedPreference:
    """The project preference apply wrote, and the queued work it re-placed.

    Attributes:
        project_id (str):
        mode (str):
        before (None | str | Unset):
        after (None | str | Unset):
        changed (bool | Unset):  Default: False.
        applied (bool | None | Unset):
        placement (None | ProviderAllocationPlacement | Unset):
    """

    project_id: str
    mode: str
    before: None | str | Unset = UNSET
    after: None | str | Unset = UNSET
    changed: bool | Unset = False
    applied: bool | None | Unset = UNSET
    placement: None | ProviderAllocationPlacement | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.provider_allocation_placement import ProviderAllocationPlacement

        project_id = self.project_id

        mode = self.mode

        before: None | str | Unset
        if isinstance(self.before, Unset):
            before = UNSET
        else:
            before = self.before

        after: None | str | Unset
        if isinstance(self.after, Unset):
            after = UNSET
        else:
            after = self.after

        changed = self.changed

        applied: bool | None | Unset
        if isinstance(self.applied, Unset):
            applied = UNSET
        else:
            applied = self.applied

        placement: dict[str, Any] | None | Unset
        if isinstance(self.placement, Unset):
            placement = UNSET
        elif isinstance(self.placement, ProviderAllocationPlacement):
            placement = self.placement.to_dict()
        else:
            placement = self.placement

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "mode": mode,
            }
        )
        if before is not UNSET:
            field_dict["before"] = before
        if after is not UNSET:
            field_dict["after"] = after
        if changed is not UNSET:
            field_dict["changed"] = changed
        if applied is not UNSET:
            field_dict["applied"] = applied
        if placement is not UNSET:
            field_dict["placement"] = placement

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_placement import ProviderAllocationPlacement

        d = dict(src_dict)
        project_id = d.pop("project_id")

        mode = d.pop("mode")

        def _parse_before(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        before = _parse_before(d.pop("before", UNSET))

        def _parse_after(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        after = _parse_after(d.pop("after", UNSET))

        changed = d.pop("changed", UNSET)

        def _parse_applied(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        applied = _parse_applied(d.pop("applied", UNSET))

        def _parse_placement(data: object) -> None | ProviderAllocationPlacement | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                placement_type_0 = ProviderAllocationPlacement.from_dict(data)

                return placement_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderAllocationPlacement | Unset, data)

        placement = _parse_placement(d.pop("placement", UNSET))

        provider_allocation_applied_preference = cls(
            project_id=project_id,
            mode=mode,
            before=before,
            after=after,
            changed=changed,
            applied=applied,
            placement=placement,
        )

        provider_allocation_applied_preference.additional_properties = d
        return provider_allocation_applied_preference

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
