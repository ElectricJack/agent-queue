from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.collaboration_thread_record import CollaborationThreadRecord


T = TypeVar("T", bound="CollaborationCloseResponse")


@_attrs_define
class CollaborationCloseResponse:
    """
    Attributes:
        thread (CollaborationThreadRecord | None | Unset):
        closed_count (int | None | Unset):
        thread_ids (list[str] | None | Unset):
        success (bool | Unset):  Default: True.
    """

    thread: CollaborationThreadRecord | None | Unset = UNSET
    closed_count: int | None | Unset = UNSET
    thread_ids: list[str] | None | Unset = UNSET
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        from ..models.collaboration_thread_record import CollaborationThreadRecord

        thread: dict[str, Any] | None | Unset
        if isinstance(self.thread, Unset):
            thread = UNSET
        elif isinstance(self.thread, CollaborationThreadRecord):
            thread = self.thread.to_dict()
        else:
            thread = self.thread

        closed_count: int | None | Unset
        if isinstance(self.closed_count, Unset):
            closed_count = UNSET
        else:
            closed_count = self.closed_count

        thread_ids: list[str] | None | Unset
        if isinstance(self.thread_ids, Unset):
            thread_ids = UNSET
        elif isinstance(self.thread_ids, list):
            thread_ids = self.thread_ids

        else:
            thread_ids = self.thread_ids

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update({})
        if thread is not UNSET:
            field_dict["thread"] = thread
        if closed_count is not UNSET:
            field_dict["closed_count"] = closed_count
        if thread_ids is not UNSET:
            field_dict["thread_ids"] = thread_ids
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.collaboration_thread_record import CollaborationThreadRecord

        d = dict(src_dict)

        def _parse_thread(data: object) -> CollaborationThreadRecord | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                thread_type_0 = CollaborationThreadRecord.from_dict(data)

                return thread_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(CollaborationThreadRecord | None | Unset, data)

        thread = _parse_thread(d.pop("thread", UNSET))

        def _parse_closed_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        closed_count = _parse_closed_count(d.pop("closed_count", UNSET))

        def _parse_thread_ids(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                thread_ids_type_0 = cast(list[str], data)

                return thread_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        thread_ids = _parse_thread_ids(d.pop("thread_ids", UNSET))

        success = d.pop("success", UNSET)

        collaboration_close_response = cls(
            thread=thread,
            closed_count=closed_count,
            thread_ids=thread_ids,
            success=success,
        )

        return collaboration_close_response
