from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MemorySearchRequest")


@_attrs_define
class MemorySearchRequest:
    """
    Attributes:
        project_id (str): Project ID to search memory for
        query (str): Semantic search query
        top_k (int | Unset): Number of results to return (default 10) Default: 10.
    """

    project_id: str
    query: str
    top_k: int | Unset = 10
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        query = self.query

        top_k = self.top_k

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "query": query,
            }
        )
        if top_k is not UNSET:
            field_dict["top_k"] = top_k

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        query = d.pop("query")

        top_k = d.pop("top_k", UNSET)

        memory_search_request = cls(
            project_id=project_id,
            query=query,
            top_k=top_k,
        )

        memory_search_request.additional_properties = d
        return memory_search_request

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
