from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="LoopIterationOverlayDTO")


@_attrs_define
class LoopIterationOverlayDTO:
    """
    Attributes:
        index (int):
        item_display (str):
        outcome (None | str | Unset):
        receipt_ids (list[str] | Unset):
        started_at (float | None | Unset):
        completed_at (float | None | Unset):
    """

    index: int
    item_display: str
    outcome: None | str | Unset = UNSET
    receipt_ids: list[str] | Unset = UNSET
    started_at: float | None | Unset = UNSET
    completed_at: float | None | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        index = self.index

        item_display = self.item_display

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        receipt_ids: list[str] | Unset = UNSET
        if not isinstance(self.receipt_ids, Unset):
            receipt_ids = self.receipt_ids

        started_at: float | None | Unset
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        else:
            started_at = self.started_at

        completed_at: float | None | Unset
        if isinstance(self.completed_at, Unset):
            completed_at = UNSET
        else:
            completed_at = self.completed_at

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "index": index,
                "item_display": item_display,
            }
        )
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if receipt_ids is not UNSET:
            field_dict["receipt_ids"] = receipt_ids
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        index = d.pop("index")

        item_display = d.pop("item_display")

        def _parse_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        receipt_ids = cast(list[str], d.pop("receipt_ids", UNSET))

        def _parse_started_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))

        def _parse_completed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completed_at = _parse_completed_at(d.pop("completed_at", UNSET))

        loop_iteration_overlay_dto = cls(
            index=index,
            item_display=item_display,
            outcome=outcome,
            receipt_ids=receipt_ids,
            started_at=started_at,
            completed_at=completed_at,
        )

        return loop_iteration_overlay_dto
