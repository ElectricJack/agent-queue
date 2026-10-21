from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitCheckpointResponse")


@_attrs_define
class WaitCheckpointResponse:
    """
    Attributes:
        continuation_id (str):
        generation (int):
        success (bool | Unset):  Default: True.
        next_step (None | str | Unset):
    """

    continuation_id: str
    generation: int
    success: bool | Unset = True
    next_step: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        continuation_id = self.continuation_id

        generation = self.generation

        success = self.success

        next_step: None | str | Unset
        if isinstance(self.next_step, Unset):
            next_step = UNSET
        else:
            next_step = self.next_step

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "continuation_id": continuation_id,
                "generation": generation,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if next_step is not UNSET:
            field_dict["next_step"] = next_step

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        continuation_id = d.pop("continuation_id")

        generation = d.pop("generation")

        success = d.pop("success", UNSET)

        def _parse_next_step(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_step = _parse_next_step(d.pop("next_step", UNSET))

        wait_checkpoint_response = cls(
            continuation_id=continuation_id,
            generation=generation,
            success=success,
            next_step=next_step,
        )

        return wait_checkpoint_response
