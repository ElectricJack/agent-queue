from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.collaboration_thread_record import CollaborationThreadRecord


T = TypeVar("T", bound="CollaborationListResponse")


@_attrs_define
class CollaborationListResponse:
    """
    Attributes:
        threads (list[CollaborationThreadRecord]):
        count (int):
        success (bool | Unset):  Default: True.
    """

    threads: list[CollaborationThreadRecord]
    count: int
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        threads = []
        for threads_item_data in self.threads:
            threads_item = threads_item_data.to_dict()
            threads.append(threads_item)

        count = self.count

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "threads": threads,
                "count": count,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.collaboration_thread_record import CollaborationThreadRecord

        d = dict(src_dict)
        threads = []
        _threads = d.pop("threads")
        for threads_item_data in _threads:
            threads_item = CollaborationThreadRecord.from_dict(threads_item_data)

            threads.append(threads_item)

        count = d.pop("count")

        success = d.pop("success", UNSET)

        collaboration_list_response = cls(
            threads=threads,
            count=count,
            success=success,
        )

        return collaboration_list_response
