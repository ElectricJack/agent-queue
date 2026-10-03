from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="KnowledgeHistoryRequest")


@_attrs_define
class KnowledgeHistoryRequest:
    """
    Attributes:
        project_id (str):
        identity (str):
        before_sequence (int | None | Unset):
        limit (int | Unset):  Default: 25.
    """

    project_id: str
    identity: str
    before_sequence: int | None | Unset = UNSET
    limit: int | Unset = 25
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        identity = self.identity

        before_sequence: int | None | Unset
        if isinstance(self.before_sequence, Unset):
            before_sequence = UNSET
        else:
            before_sequence = self.before_sequence

        limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "identity": identity,
            }
        )
        if before_sequence is not UNSET:
            field_dict["before_sequence"] = before_sequence
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        identity = d.pop("identity")

        def _parse_before_sequence(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        before_sequence = _parse_before_sequence(d.pop("before_sequence", UNSET))

        limit = d.pop("limit", UNSET)

        knowledge_history_request = cls(
            project_id=project_id,
            identity=identity,
            before_sequence=before_sequence,
            limit=limit,
        )

        knowledge_history_request.additional_properties = d
        return knowledge_history_request

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
