from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.job_logs_response_chunks_item import JobLogsResponseChunksItem
    from ..models.job_logs_response_gaps_item import JobLogsResponseGapsItem


T = TypeVar("T", bound="JobLogsResponse")


@_attrs_define
class JobLogsResponse:
    """
    Attributes:
        chunks (list[JobLogsResponseChunksItem]):
        gaps (list[JobLogsResponseGapsItem]):
        next_ (int):
        seen (int):
        success (bool | Unset):  Default: True.
    """

    chunks: list[JobLogsResponseChunksItem]
    gaps: list[JobLogsResponseGapsItem]
    next_: int
    seen: int
    success: bool | Unset = True

    def to_dict(self) -> dict[str, Any]:
        chunks = []
        for chunks_item_data in self.chunks:
            chunks_item = chunks_item_data.to_dict()
            chunks.append(chunks_item)

        gaps = []
        for gaps_item_data in self.gaps:
            gaps_item = gaps_item_data.to_dict()
            gaps.append(gaps_item)

        next_ = self.next_

        seen = self.seen

        success = self.success

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "chunks": chunks,
                "gaps": gaps,
                "next": next_,
                "seen": seen,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_logs_response_chunks_item import JobLogsResponseChunksItem
        from ..models.job_logs_response_gaps_item import JobLogsResponseGapsItem

        d = dict(src_dict)
        chunks = []
        _chunks = d.pop("chunks")
        for chunks_item_data in _chunks:
            chunks_item = JobLogsResponseChunksItem.from_dict(chunks_item_data)

            chunks.append(chunks_item)

        gaps = []
        _gaps = d.pop("gaps")
        for gaps_item_data in _gaps:
            gaps_item = JobLogsResponseGapsItem.from_dict(gaps_item_data)

            gaps.append(gaps_item)

        next_ = d.pop("next")

        seen = d.pop("seen")

        success = d.pop("success", UNSET)

        job_logs_response = cls(
            chunks=chunks,
            gaps=gaps,
            next_=next_,
            seen=seen,
            success=success,
        )

        return job_logs_response
