from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MemoryReindexResponse")


@_attrs_define
class MemoryReindexResponse:
    """
    Attributes:
        project_id (str):
        status (str | Unset):  Default: 'reindex_complete'.
        chunks_indexed (int | Unset):  Default: 0.
    """

    project_id: str
    status: str | Unset = "reindex_complete"
    chunks_indexed: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        status = self.status

        chunks_indexed = self.chunks_indexed

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if chunks_indexed is not UNSET:
            field_dict["chunks_indexed"] = chunks_indexed

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        status = d.pop("status", UNSET)

        chunks_indexed = d.pop("chunks_indexed", UNSET)

        memory_reindex_response = cls(
            project_id=project_id,
            status=status,
            chunks_indexed=chunks_indexed,
        )

        memory_reindex_response.additional_properties = d
        return memory_reindex_response

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
