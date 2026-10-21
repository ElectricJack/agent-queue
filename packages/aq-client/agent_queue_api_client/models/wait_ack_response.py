from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitAckResponse")


@_attrs_define
class WaitAckResponse:
    """
    Attributes:
        wait_id (str):
        acknowledged (bool):
        success (bool | Unset):  Default: True.
        continuation_id (None | str | Unset):
        generation (int | None | Unset):
        next_step (None | str | Unset):
    """

    wait_id: str
    acknowledged: bool
    success: bool | Unset = True
    continuation_id: None | str | Unset = UNSET
    generation: int | None | Unset = UNSET
    next_step: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        wait_id = self.wait_id

        acknowledged = self.acknowledged

        success = self.success

        continuation_id: None | str | Unset
        if isinstance(self.continuation_id, Unset):
            continuation_id = UNSET
        else:
            continuation_id = self.continuation_id

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
                "acknowledged": acknowledged,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if continuation_id is not UNSET:
            field_dict["continuation_id"] = continuation_id
        if generation is not UNSET:
            field_dict["generation"] = generation
        if next_step is not UNSET:
            field_dict["next_step"] = next_step

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        wait_id = d.pop("wait_id")

        acknowledged = d.pop("acknowledged")

        success = d.pop("success", UNSET)

        def _parse_continuation_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        continuation_id = _parse_continuation_id(d.pop("continuation_id", UNSET))

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

        wait_ack_response = cls(
            wait_id=wait_id,
            acknowledged=acknowledged,
            success=success,
            continuation_id=continuation_id,
            generation=generation,
            next_step=next_step,
        )

        return wait_ack_response
