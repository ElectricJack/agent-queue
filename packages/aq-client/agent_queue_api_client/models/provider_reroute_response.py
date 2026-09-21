from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.provider_reroute_response_held_by_kind import ProviderRerouteResponseHeldByKind
    from ..models.reroute_decision import RerouteDecision


T = TypeVar("T", bound="ProviderRerouteResponse")


@_attrs_define
class ProviderRerouteResponse:
    """``provider_reroute``: one sweep's plan and what it applied (D11).

    ``outcome`` is ``rerouted``, ``held``, ``idle`` or ``disabled``.  With
    ``dry_run`` (or while re-routing is off) ``applied`` is false and ``moved``
    lists what a live sweep would move.

        Attributes:
            outcome (str):
            success (bool | Unset):  Default: True.
            dry_run (bool | Unset):  Default: False.
            applied (bool | Unset):  Default: False.
            disabled_reason (None | str | Unset):
            unavailable_providers (list[str] | Unset):
            moved (list[RerouteDecision] | Unset):
            held (list[RerouteDecision] | Unset):
            held_by_kind (ProviderRerouteResponseHeldByKind | Unset):
            resumed (list[str] | Unset):
            lost (list[str] | Unset):
            skipped (list[RerouteDecision] | Unset):
            batch_ids (list[str] | Unset):
            notices (list[str] | Unset):
    """

    outcome: str
    success: bool | Unset = True
    dry_run: bool | Unset = False
    applied: bool | Unset = False
    disabled_reason: None | str | Unset = UNSET
    unavailable_providers: list[str] | Unset = UNSET
    moved: list[RerouteDecision] | Unset = UNSET
    held: list[RerouteDecision] | Unset = UNSET
    held_by_kind: ProviderRerouteResponseHeldByKind | Unset = UNSET
    resumed: list[str] | Unset = UNSET
    lost: list[str] | Unset = UNSET
    skipped: list[RerouteDecision] | Unset = UNSET
    batch_ids: list[str] | Unset = UNSET
    notices: list[str] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        outcome = self.outcome

        success = self.success

        dry_run = self.dry_run

        applied = self.applied

        disabled_reason: None | str | Unset
        if isinstance(self.disabled_reason, Unset):
            disabled_reason = UNSET
        else:
            disabled_reason = self.disabled_reason

        unavailable_providers: list[str] | Unset = UNSET
        if not isinstance(self.unavailable_providers, Unset):
            unavailable_providers = self.unavailable_providers

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

        held_by_kind: dict[str, Any] | Unset = UNSET
        if not isinstance(self.held_by_kind, Unset):
            held_by_kind = self.held_by_kind.to_dict()

        resumed: list[str] | Unset = UNSET
        if not isinstance(self.resumed, Unset):
            resumed = self.resumed

        lost: list[str] | Unset = UNSET
        if not isinstance(self.lost, Unset):
            lost = self.lost

        skipped: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.skipped, Unset):
            skipped = []
            for skipped_item_data in self.skipped:
                skipped_item = skipped_item_data.to_dict()
                skipped.append(skipped_item)

        batch_ids: list[str] | Unset = UNSET
        if not isinstance(self.batch_ids, Unset):
            batch_ids = self.batch_ids

        notices: list[str] | Unset = UNSET
        if not isinstance(self.notices, Unset):
            notices = self.notices

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "outcome": outcome,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if applied is not UNSET:
            field_dict["applied"] = applied
        if disabled_reason is not UNSET:
            field_dict["disabled_reason"] = disabled_reason
        if unavailable_providers is not UNSET:
            field_dict["unavailable_providers"] = unavailable_providers
        if moved is not UNSET:
            field_dict["moved"] = moved
        if held is not UNSET:
            field_dict["held"] = held
        if held_by_kind is not UNSET:
            field_dict["held_by_kind"] = held_by_kind
        if resumed is not UNSET:
            field_dict["resumed"] = resumed
        if lost is not UNSET:
            field_dict["lost"] = lost
        if skipped is not UNSET:
            field_dict["skipped"] = skipped
        if batch_ids is not UNSET:
            field_dict["batch_ids"] = batch_ids
        if notices is not UNSET:
            field_dict["notices"] = notices

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.provider_reroute_response_held_by_kind import ProviderRerouteResponseHeldByKind
        from ..models.reroute_decision import RerouteDecision

        d = dict(src_dict)
        outcome = d.pop("outcome")

        success = d.pop("success", UNSET)

        dry_run = d.pop("dry_run", UNSET)

        applied = d.pop("applied", UNSET)

        def _parse_disabled_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        disabled_reason = _parse_disabled_reason(d.pop("disabled_reason", UNSET))

        unavailable_providers = cast(list[str], d.pop("unavailable_providers", UNSET))

        _moved = d.pop("moved", UNSET)
        moved: list[RerouteDecision] | Unset = UNSET
        if _moved is not UNSET:
            moved = []
            for moved_item_data in _moved:
                moved_item = RerouteDecision.from_dict(moved_item_data)

                moved.append(moved_item)

        _held = d.pop("held", UNSET)
        held: list[RerouteDecision] | Unset = UNSET
        if _held is not UNSET:
            held = []
            for held_item_data in _held:
                held_item = RerouteDecision.from_dict(held_item_data)

                held.append(held_item)

        _held_by_kind = d.pop("held_by_kind", UNSET)
        held_by_kind: ProviderRerouteResponseHeldByKind | Unset
        if isinstance(_held_by_kind, Unset):
            held_by_kind = UNSET
        else:
            held_by_kind = ProviderRerouteResponseHeldByKind.from_dict(_held_by_kind)

        resumed = cast(list[str], d.pop("resumed", UNSET))

        lost = cast(list[str], d.pop("lost", UNSET))

        _skipped = d.pop("skipped", UNSET)
        skipped: list[RerouteDecision] | Unset = UNSET
        if _skipped is not UNSET:
            skipped = []
            for skipped_item_data in _skipped:
                skipped_item = RerouteDecision.from_dict(skipped_item_data)

                skipped.append(skipped_item)

        batch_ids = cast(list[str], d.pop("batch_ids", UNSET))

        notices = cast(list[str], d.pop("notices", UNSET))

        provider_reroute_response = cls(
            outcome=outcome,
            success=success,
            dry_run=dry_run,
            applied=applied,
            disabled_reason=disabled_reason,
            unavailable_providers=unavailable_providers,
            moved=moved,
            held=held,
            held_by_kind=held_by_kind,
            resumed=resumed,
            lost=lost,
            skipped=skipped,
            batch_ids=batch_ids,
            notices=notices,
        )

        provider_reroute_response.additional_properties = d
        return provider_reroute_response

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
