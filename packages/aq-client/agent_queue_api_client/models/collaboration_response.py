from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.collaboration_thread_record import CollaborationThreadRecord


T = TypeVar("T", bound="CollaborationResponse")


@_attrs_define
class CollaborationResponse:
    """
    Attributes:
        thread (CollaborationThreadRecord):
        replayed (bool | None | Unset):
        next_step (None | str | Unset):
        success (bool | Unset):  Default: True.
    """

    thread: CollaborationThreadRecord
    replayed: bool | None | Unset = UNSET
    next_step: None | str | Unset = UNSET
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        thread = self.thread.to_dict()

        replayed: bool | None | Unset
        if isinstance(self.replayed, Unset):
            replayed = UNSET
        else:
            replayed = self.replayed

        next_step: None | str | Unset
        if isinstance(self.next_step, Unset):
            next_step = UNSET
        else:
            next_step = self.next_step

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "thread": thread,
            }
        )
        if replayed is not UNSET:
            field_dict["replayed"] = replayed
        if next_step is not UNSET:
            field_dict["next_step"] = next_step
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.collaboration_thread_record import CollaborationThreadRecord

        d = dict(src_dict)
        thread = CollaborationThreadRecord.from_dict(d.pop("thread"))

        def _parse_replayed(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        replayed = _parse_replayed(d.pop("replayed", UNSET))

        def _parse_next_step(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_step = _parse_next_step(d.pop("next_step", UNSET))

        success = d.pop("success", UNSET)

        collaboration_response = cls(
            thread=thread,
            replayed=replayed,
            next_step=next_step,
            success=success,
        )

        return collaboration_response
