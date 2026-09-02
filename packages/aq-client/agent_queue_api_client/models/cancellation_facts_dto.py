from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="CancellationFactsDTO")


@_attrs_define
class CancellationFactsDTO:
    """
    Attributes:
        requested_at (float):
        acknowledged_at (float | None | Unset):
        cancelled_child (bool | Unset):  Default: False.
    """

    requested_at: float
    acknowledged_at: float | None | Unset = UNSET
    cancelled_child: bool | Unset = False

    def to_dict(self) -> dict[str, Any]:
        requested_at = self.requested_at

        acknowledged_at: float | None | Unset
        if isinstance(self.acknowledged_at, Unset):
            acknowledged_at = UNSET
        else:
            acknowledged_at = self.acknowledged_at

        cancelled_child = self.cancelled_child

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "requested_at": requested_at,
            }
        )
        if acknowledged_at is not UNSET:
            field_dict["acknowledged_at"] = acknowledged_at
        if cancelled_child is not UNSET:
            field_dict["cancelled_child"] = cancelled_child

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        requested_at = d.pop("requested_at")

        def _parse_acknowledged_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        acknowledged_at = _parse_acknowledged_at(d.pop("acknowledged_at", UNSET))

        cancelled_child = d.pop("cancelled_child", UNSET)

        cancellation_facts_dto = cls(
            requested_at=requested_at,
            acknowledged_at=acknowledged_at,
            cancelled_child=cancelled_child,
        )

        return cancellation_facts_dto
