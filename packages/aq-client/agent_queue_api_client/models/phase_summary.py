from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.phase_hold_detail import PhaseHoldDetail


T = TypeVar("T", bound="PhaseSummary")


@_attrs_define
class PhaseSummary:
    """One phase as ``phase_list`` reports it.

    Attributes:
        id (str):
        title (str):
        label (str):
        order (int):
        status (str):
        is_blocked (bool | Unset):  Default: False.
        total (int | Unset):  Default: 0.
        done (int | Unset):  Default: 0.
        phase_hold (None | PhaseHoldDetail | Unset):
    """

    id: str
    title: str
    label: str
    order: int
    status: str
    is_blocked: bool | Unset = False
    total: int | Unset = 0
    done: int | Unset = 0
    phase_hold: None | PhaseHoldDetail | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.phase_hold_detail import PhaseHoldDetail

        id = self.id

        title = self.title

        label = self.label

        order = self.order

        status = self.status

        is_blocked = self.is_blocked

        total = self.total

        done = self.done

        phase_hold: dict[str, Any] | None | Unset
        if isinstance(self.phase_hold, Unset):
            phase_hold = UNSET
        elif isinstance(self.phase_hold, PhaseHoldDetail):
            phase_hold = self.phase_hold.to_dict()
        else:
            phase_hold = self.phase_hold

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "title": title,
                "label": label,
                "order": order,
                "status": status,
            }
        )
        if is_blocked is not UNSET:
            field_dict["is_blocked"] = is_blocked
        if total is not UNSET:
            field_dict["total"] = total
        if done is not UNSET:
            field_dict["done"] = done
        if phase_hold is not UNSET:
            field_dict["phase_hold"] = phase_hold

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.phase_hold_detail import PhaseHoldDetail

        d = dict(src_dict)
        id = d.pop("id")

        title = d.pop("title")

        label = d.pop("label")

        order = d.pop("order")

        status = d.pop("status")

        is_blocked = d.pop("is_blocked", UNSET)

        total = d.pop("total", UNSET)

        done = d.pop("done", UNSET)

        def _parse_phase_hold(data: object) -> None | PhaseHoldDetail | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                phase_hold_type_0 = PhaseHoldDetail.from_dict(data)

                return phase_hold_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PhaseHoldDetail | Unset, data)

        phase_hold = _parse_phase_hold(d.pop("phase_hold", UNSET))

        phase_summary = cls(
            id=id,
            title=title,
            label=label,
            order=order,
            status=status,
            is_blocked=is_blocked,
            total=total,
            done=done,
            phase_hold=phase_hold,
        )

        phase_summary.additional_properties = d
        return phase_summary

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
