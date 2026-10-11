from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.epic_delivery_status_display_state_type_0 import EpicDeliveryStatusDisplayStateType0
from ..models.epic_delivery_status_evidence import EpicDeliveryStatusEvidence
from ..models.epic_delivery_status_hold_type_0 import EpicDeliveryStatusHoldType0
from ..models.epic_delivery_status_state import EpicDeliveryStatusState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.epic_delivery_ref import EpicDeliveryRef
    from ..models.epic_delivery_status_child_state_counts_type_0 import EpicDeliveryStatusChildStateCountsType0
    from ..models.epic_delivery_status_descendant_state_counts_type_0 import (
        EpicDeliveryStatusDescendantStateCountsType0,
    )


T = TypeVar("T", bound="EpicDeliveryStatus")


@_attrs_define
class EpicDeliveryStatus:
    """Implementation progress kept apart from delivery for a task with children.

    A read-only display projection (``src/integration/epic_delivery.py``): it
    never changes the stored lifecycle and authorizes nothing. ``state`` is
    ``integrating``/``verifying`` only with a live, recently active session,
    and ``delivered`` only with evidence for the epic's *current* completion:
    git's own request-scoped answer that it is on the project's target, or a
    receipt binding the current head.
    ``display_status`` replaces the stored status on an epic card, so
    ``Paused`` appears only for an operator hold (``hold == "operator"``).

        Attributes:
            state (EpicDeliveryStatusState):
            label (str):
            display_status (str):
            display_state (EpicDeliveryStatusDisplayStateType0 | None | Unset):
            display_reason (None | str | Unset):
            child_state_counts (EpicDeliveryStatusChildStateCountsType0 | None | Unset):
            descendant_state_counts (EpicDeliveryStatusDescendantStateCountsType0 | None | Unset):
            hold (EpicDeliveryStatusHoldType0 | None | Unset):
            reason (None | str | Unset):
            remedy (None | str | Unset):
            responsible (EpicDeliveryRef | None | Unset):
            since (float | None | Unset):
            links (list[EpicDeliveryRef] | Unset):
            evidence (EpicDeliveryStatusEvidence | Unset):  Default: EpicDeliveryStatusEvidence.CURRENT.
            implementation_completed (int | Unset):  Default: 0.
            implementation_total (int | Unset):  Default: 0.
    """

    state: EpicDeliveryStatusState
    label: str
    display_status: str
    display_state: EpicDeliveryStatusDisplayStateType0 | None | Unset = UNSET
    display_reason: None | str | Unset = UNSET
    child_state_counts: EpicDeliveryStatusChildStateCountsType0 | None | Unset = UNSET
    descendant_state_counts: EpicDeliveryStatusDescendantStateCountsType0 | None | Unset = UNSET
    hold: EpicDeliveryStatusHoldType0 | None | Unset = UNSET
    reason: None | str | Unset = UNSET
    remedy: None | str | Unset = UNSET
    responsible: EpicDeliveryRef | None | Unset = UNSET
    since: float | None | Unset = UNSET
    links: list[EpicDeliveryRef] | Unset = UNSET
    evidence: EpicDeliveryStatusEvidence | Unset = EpicDeliveryStatusEvidence.CURRENT
    implementation_completed: int | Unset = 0
    implementation_total: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.epic_delivery_ref import EpicDeliveryRef
        from ..models.epic_delivery_status_child_state_counts_type_0 import EpicDeliveryStatusChildStateCountsType0
        from ..models.epic_delivery_status_descendant_state_counts_type_0 import (
            EpicDeliveryStatusDescendantStateCountsType0,
        )

        state = self.state.value

        label = self.label

        display_status = self.display_status

        display_state: None | str | Unset
        if isinstance(self.display_state, Unset):
            display_state = UNSET
        elif isinstance(self.display_state, EpicDeliveryStatusDisplayStateType0):
            display_state = self.display_state.value
        else:
            display_state = self.display_state

        display_reason: None | str | Unset
        if isinstance(self.display_reason, Unset):
            display_reason = UNSET
        else:
            display_reason = self.display_reason

        child_state_counts: dict[str, Any] | None | Unset
        if isinstance(self.child_state_counts, Unset):
            child_state_counts = UNSET
        elif isinstance(self.child_state_counts, EpicDeliveryStatusChildStateCountsType0):
            child_state_counts = self.child_state_counts.to_dict()
        else:
            child_state_counts = self.child_state_counts

        descendant_state_counts: dict[str, Any] | None | Unset
        if isinstance(self.descendant_state_counts, Unset):
            descendant_state_counts = UNSET
        elif isinstance(self.descendant_state_counts, EpicDeliveryStatusDescendantStateCountsType0):
            descendant_state_counts = self.descendant_state_counts.to_dict()
        else:
            descendant_state_counts = self.descendant_state_counts

        hold: None | str | Unset
        if isinstance(self.hold, Unset):
            hold = UNSET
        elif isinstance(self.hold, EpicDeliveryStatusHoldType0):
            hold = self.hold.value
        else:
            hold = self.hold

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        remedy: None | str | Unset
        if isinstance(self.remedy, Unset):
            remedy = UNSET
        else:
            remedy = self.remedy

        responsible: dict[str, Any] | None | Unset
        if isinstance(self.responsible, Unset):
            responsible = UNSET
        elif isinstance(self.responsible, EpicDeliveryRef):
            responsible = self.responsible.to_dict()
        else:
            responsible = self.responsible

        since: float | None | Unset
        if isinstance(self.since, Unset):
            since = UNSET
        else:
            since = self.since

        links: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.links, Unset):
            links = []
            for links_item_data in self.links:
                links_item = links_item_data.to_dict()
                links.append(links_item)

        evidence: str | Unset = UNSET
        if not isinstance(self.evidence, Unset):
            evidence = self.evidence.value

        implementation_completed = self.implementation_completed

        implementation_total = self.implementation_total

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "state": state,
                "label": label,
                "display_status": display_status,
            }
        )
        if display_state is not UNSET:
            field_dict["display_state"] = display_state
        if display_reason is not UNSET:
            field_dict["display_reason"] = display_reason
        if child_state_counts is not UNSET:
            field_dict["child_state_counts"] = child_state_counts
        if descendant_state_counts is not UNSET:
            field_dict["descendant_state_counts"] = descendant_state_counts
        if hold is not UNSET:
            field_dict["hold"] = hold
        if reason is not UNSET:
            field_dict["reason"] = reason
        if remedy is not UNSET:
            field_dict["remedy"] = remedy
        if responsible is not UNSET:
            field_dict["responsible"] = responsible
        if since is not UNSET:
            field_dict["since"] = since
        if links is not UNSET:
            field_dict["links"] = links
        if evidence is not UNSET:
            field_dict["evidence"] = evidence
        if implementation_completed is not UNSET:
            field_dict["implementation_completed"] = implementation_completed
        if implementation_total is not UNSET:
            field_dict["implementation_total"] = implementation_total

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.epic_delivery_ref import EpicDeliveryRef
        from ..models.epic_delivery_status_child_state_counts_type_0 import EpicDeliveryStatusChildStateCountsType0
        from ..models.epic_delivery_status_descendant_state_counts_type_0 import (
            EpicDeliveryStatusDescendantStateCountsType0,
        )

        d = dict(src_dict)
        state = EpicDeliveryStatusState(d.pop("state"))

        label = d.pop("label")

        display_status = d.pop("display_status")

        def _parse_display_state(data: object) -> EpicDeliveryStatusDisplayStateType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                display_state_type_0 = EpicDeliveryStatusDisplayStateType0(data)

                return display_state_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EpicDeliveryStatusDisplayStateType0 | None | Unset, data)

        display_state = _parse_display_state(d.pop("display_state", UNSET))

        def _parse_display_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        display_reason = _parse_display_reason(d.pop("display_reason", UNSET))

        def _parse_child_state_counts(data: object) -> EpicDeliveryStatusChildStateCountsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                child_state_counts_type_0 = EpicDeliveryStatusChildStateCountsType0.from_dict(data)

                return child_state_counts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EpicDeliveryStatusChildStateCountsType0 | None | Unset, data)

        child_state_counts = _parse_child_state_counts(d.pop("child_state_counts", UNSET))

        def _parse_descendant_state_counts(data: object) -> EpicDeliveryStatusDescendantStateCountsType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                descendant_state_counts_type_0 = EpicDeliveryStatusDescendantStateCountsType0.from_dict(data)

                return descendant_state_counts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EpicDeliveryStatusDescendantStateCountsType0 | None | Unset, data)

        descendant_state_counts = _parse_descendant_state_counts(d.pop("descendant_state_counts", UNSET))

        def _parse_hold(data: object) -> EpicDeliveryStatusHoldType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                hold_type_0 = EpicDeliveryStatusHoldType0(data)

                return hold_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EpicDeliveryStatusHoldType0 | None | Unset, data)

        hold = _parse_hold(d.pop("hold", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_remedy(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        remedy = _parse_remedy(d.pop("remedy", UNSET))

        def _parse_responsible(data: object) -> EpicDeliveryRef | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                responsible_type_0 = EpicDeliveryRef.from_dict(data)

                return responsible_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(EpicDeliveryRef | None | Unset, data)

        responsible = _parse_responsible(d.pop("responsible", UNSET))

        def _parse_since(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        since = _parse_since(d.pop("since", UNSET))

        _links = d.pop("links", UNSET)
        links: list[EpicDeliveryRef] | Unset = UNSET
        if _links is not UNSET:
            links = []
            for links_item_data in _links:
                links_item = EpicDeliveryRef.from_dict(links_item_data)

                links.append(links_item)

        _evidence = d.pop("evidence", UNSET)
        evidence: EpicDeliveryStatusEvidence | Unset
        if isinstance(_evidence, Unset):
            evidence = UNSET
        else:
            evidence = EpicDeliveryStatusEvidence(_evidence)

        implementation_completed = d.pop("implementation_completed", UNSET)

        implementation_total = d.pop("implementation_total", UNSET)

        epic_delivery_status = cls(
            state=state,
            label=label,
            display_status=display_status,
            display_state=display_state,
            display_reason=display_reason,
            child_state_counts=child_state_counts,
            descendant_state_counts=descendant_state_counts,
            hold=hold,
            reason=reason,
            remedy=remedy,
            responsible=responsible,
            since=since,
            links=links,
            evidence=evidence,
            implementation_completed=implementation_completed,
            implementation_total=implementation_total,
        )

        epic_delivery_status.additional_properties = d
        return epic_delivery_status

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
