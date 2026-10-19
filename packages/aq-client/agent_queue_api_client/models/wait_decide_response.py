from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitDecideResponse")


@_attrs_define
class WaitDecideResponse:
    """
    Attributes:
        wait_id (str):
        decision (str):
        success (bool | Unset):  Default: True.
        outcome (None | str | Unset):
        generation (int | None | Unset):
        next_step (None | str | Unset):
    """

    wait_id: str
    decision: str
    success: bool | Unset = True
    outcome: None | str | Unset = UNSET
    generation: int | None | Unset = UNSET
    next_step: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        wait_id = self.wait_id

        decision = self.decision

        success = self.success

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        generation: int | None | Unset
        if isinstance(self.generation, Unset):
            generation = UNSET
        else:
            generation = self.generation

        next_step: None | str | Unset
        if isinstance(self.next_step, Unset):
            next_step = UNSET
        else:
            next_step = self.next_step

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "wait_id": wait_id,
                "decision": decision,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if generation is not UNSET:
            field_dict["generation"] = generation
        if next_step is not UNSET:
            field_dict["next_step"] = next_step

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        wait_id = d.pop("wait_id")

        decision = d.pop("decision")

        success = d.pop("success", UNSET)

        def _parse_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_generation(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        generation = _parse_generation(d.pop("generation", UNSET))

        def _parse_next_step(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_step = _parse_next_step(d.pop("next_step", UNSET))

        wait_decide_response = cls(
            wait_id=wait_id,
            decision=decision,
            success=success,
            outcome=outcome,
            generation=generation,
            next_step=next_step,
        )

        return wait_decide_response
