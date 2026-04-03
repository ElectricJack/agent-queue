from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MemorySearchResult")


@_attrs_define
class MemorySearchResult:
    """
    Attributes:
        rank (int | Unset):  Default: 0.
        source (str | Unset):  Default: ''.
        heading (str | Unset):  Default: ''.
        content (str | Unset):  Default: ''.
        score (float | Unset):  Default: 0.0.
    """

    rank: int | Unset = 0
    source: str | Unset = ""
    heading: str | Unset = ""
    content: str | Unset = ""
    score: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        rank = self.rank

        source = self.source

        heading = self.heading

        content = self.content

        score = self.score

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if rank is not UNSET:
            field_dict["rank"] = rank
        if source is not UNSET:
            field_dict["source"] = source
        if heading is not UNSET:
            field_dict["heading"] = heading
        if content is not UNSET:
            field_dict["content"] = content
        if score is not UNSET:
            field_dict["score"] = score

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        rank = d.pop("rank", UNSET)

        source = d.pop("source", UNSET)

        heading = d.pop("heading", UNSET)

        content = d.pop("content", UNSET)

        score = d.pop("score", UNSET)

        memory_search_result = cls(
            rank=rank,
            source=source,
            heading=heading,
            content=content,
            score=score,
        )

        memory_search_result.additional_properties = d
        return memory_search_result

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
