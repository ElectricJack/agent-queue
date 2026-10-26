from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="WaitSourceRedispatchArgs")


@_attrs_define
class WaitSourceRedispatchArgs:
    """
    Attributes:
        project_id (str):
        source_seq (int):
        note (str):
    """

    project_id: str
    source_seq: int
    note: str

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        source_seq = self.source_seq

        note = self.note

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "project_id": project_id,
                "source_seq": source_seq,
                "note": note,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        source_seq = d.pop("source_seq")

        note = d.pop("note")

        wait_source_redispatch_args = cls(
            project_id=project_id,
            source_seq=source_seq,
            note=note,
        )

        return wait_source_redispatch_args
