from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitPriorityMetrics")


@_attrs_define
class WaitPriorityMetrics:
    """
    Attributes:
        donors (float | None | Unset):
        reservations_pending (float | None | Unset):
        reservations_overdue (float | None | Unset):
        deadline_breaches_1h (float | None | Unset):
        unexplained (float | None | Unset):
    """

    donors: float | None | Unset = UNSET
    reservations_pending: float | None | Unset = UNSET
    reservations_overdue: float | None | Unset = UNSET
    deadline_breaches_1h: float | None | Unset = UNSET
    unexplained: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        donors: float | None | Unset
        if isinstance(self.donors, Unset):
            donors = UNSET
        else:
            donors = self.donors

        reservations_pending: float | None | Unset
        if isinstance(self.reservations_pending, Unset):
            reservations_pending = UNSET
        else:
            reservations_pending = self.reservations_pending

        reservations_overdue: float | None | Unset
        if isinstance(self.reservations_overdue, Unset):
            reservations_overdue = UNSET
        else:
            reservations_overdue = self.reservations_overdue

        deadline_breaches_1h: float | None | Unset
        if isinstance(self.deadline_breaches_1h, Unset):
            deadline_breaches_1h = UNSET
        else:
            deadline_breaches_1h = self.deadline_breaches_1h

        unexplained: float | None | Unset
        if isinstance(self.unexplained, Unset):
            unexplained = UNSET
        else:
            unexplained = self.unexplained

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if donors is not UNSET:
            field_dict["donors"] = donors
        if reservations_pending is not UNSET:
            field_dict["reservations_pending"] = reservations_pending
        if reservations_overdue is not UNSET:
            field_dict["reservations_overdue"] = reservations_overdue
        if deadline_breaches_1h is not UNSET:
            field_dict["deadline_breaches_1h"] = deadline_breaches_1h
        if unexplained is not UNSET:
            field_dict["unexplained"] = unexplained

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_donors(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        donors = _parse_donors(d.pop("donors", UNSET))

        def _parse_reservations_pending(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        reservations_pending = _parse_reservations_pending(d.pop("reservations_pending", UNSET))

        def _parse_reservations_overdue(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        reservations_overdue = _parse_reservations_overdue(d.pop("reservations_overdue", UNSET))

        def _parse_deadline_breaches_1h(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        deadline_breaches_1h = _parse_deadline_breaches_1h(d.pop("deadline_breaches_1h", UNSET))

        def _parse_unexplained(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        unexplained = _parse_unexplained(d.pop("unexplained", UNSET))

        wait_priority_metrics = cls(
            donors=donors,
            reservations_pending=reservations_pending,
            reservations_overdue=reservations_overdue,
            deadline_breaches_1h=deadline_breaches_1h,
            unexplained=unexplained,
        )

        wait_priority_metrics.additional_properties = d
        return wait_priority_metrics

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
