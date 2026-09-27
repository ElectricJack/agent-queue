from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_allocation_placement_held_item import ProviderAllocationPlacementHeldItem
    from ..models.provider_allocation_placement_moved_item import ProviderAllocationPlacementMovedItem
    from ..models.provider_allocation_placement_skipped_item import ProviderAllocationPlacementSkippedItem


T = TypeVar("T", bound="ProviderAllocationPlacement")


@_attrs_define
class ProviderAllocationPlacement:
    """The queued ``class_only`` READY tasks a ``prefer`` moved to the provider.

    ``moved`` / ``held`` / ``skipped`` are ``provider_reroute`` decisions;
    ``batch_ids`` undo with ``provider_reroute_undo``.

        Attributes:
            applied (bool | Unset):  Default: False.
            moved (list[ProviderAllocationPlacementMovedItem] | Unset):
            held (list[ProviderAllocationPlacementHeldItem] | Unset):
            skipped (list[ProviderAllocationPlacementSkippedItem] | Unset):
            batch_ids (list[str] | Unset):
            detail (None | str | Unset):
            errors (list[str] | Unset):
    """

    applied: bool | Unset = False
    moved: list[ProviderAllocationPlacementMovedItem] | Unset = UNSET
    held: list[ProviderAllocationPlacementHeldItem] | Unset = UNSET
    skipped: list[ProviderAllocationPlacementSkippedItem] | Unset = UNSET
    batch_ids: list[str] | Unset = UNSET
    detail: None | str | Unset = UNSET
    errors: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        applied = self.applied

        moved: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.moved, Unset):
            moved = []
            for moved_item_data in self.moved:
                moved_item = moved_item_data.to_dict()
                moved.append(moved_item)

        held: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.held, Unset):
            held = []
            for held_item_data in self.held:
                held_item = held_item_data.to_dict()
                held.append(held_item)

        skipped: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.skipped, Unset):
            skipped = []
            for skipped_item_data in self.skipped:
                skipped_item = skipped_item_data.to_dict()
                skipped.append(skipped_item)

        batch_ids: list[str] | Unset = UNSET
        if not isinstance(self.batch_ids, Unset):
            batch_ids = self.batch_ids

        detail: None | str | Unset
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail

        errors: list[str] | Unset = UNSET
        if not isinstance(self.errors, Unset):
            errors = self.errors

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if applied is not UNSET:
            field_dict["applied"] = applied
        if moved is not UNSET:
            field_dict["moved"] = moved
        if held is not UNSET:
            field_dict["held"] = held
        if skipped is not UNSET:
            field_dict["skipped"] = skipped
        if batch_ids is not UNSET:
            field_dict["batch_ids"] = batch_ids
        if detail is not UNSET:
            field_dict["detail"] = detail
        if errors is not UNSET:
            field_dict["errors"] = errors

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_allocation_placement_held_item import ProviderAllocationPlacementHeldItem
        from ..models.provider_allocation_placement_moved_item import ProviderAllocationPlacementMovedItem
        from ..models.provider_allocation_placement_skipped_item import ProviderAllocationPlacementSkippedItem

        d = dict(src_dict)
        applied = d.pop("applied", UNSET)

        _moved = d.pop("moved", UNSET)
        moved: list[ProviderAllocationPlacementMovedItem] | Unset = UNSET
        if _moved is not UNSET:
            moved = []
            for moved_item_data in _moved:
                moved_item = ProviderAllocationPlacementMovedItem.from_dict(moved_item_data)

                moved.append(moved_item)

        _held = d.pop("held", UNSET)
        held: list[ProviderAllocationPlacementHeldItem] | Unset = UNSET
        if _held is not UNSET:
            held = []
            for held_item_data in _held:
                held_item = ProviderAllocationPlacementHeldItem.from_dict(held_item_data)

                held.append(held_item)

        _skipped = d.pop("skipped", UNSET)
        skipped: list[ProviderAllocationPlacementSkippedItem] | Unset = UNSET
        if _skipped is not UNSET:
            skipped = []
            for skipped_item_data in _skipped:
                skipped_item = ProviderAllocationPlacementSkippedItem.from_dict(skipped_item_data)

                skipped.append(skipped_item)

        batch_ids = cast(list[str], d.pop("batch_ids", UNSET))

        def _parse_detail(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        detail = _parse_detail(d.pop("detail", UNSET))

        errors = cast(list[str], d.pop("errors", UNSET))

        provider_allocation_placement = cls(
            applied=applied,
            moved=moved,
            held=held,
            skipped=skipped,
            batch_ids=batch_ids,
            detail=detail,
            errors=errors,
        )

        provider_allocation_placement.additional_properties = d
        return provider_allocation_placement

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
