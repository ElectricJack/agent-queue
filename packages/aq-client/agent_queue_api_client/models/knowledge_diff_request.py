from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="KnowledgeDiffRequest")


@_attrs_define
class KnowledgeDiffRequest:
    """
    Attributes:
        project_id (str):
        identity (str):
        from_revision (str):
        to_revision (str):
    """

    project_id: str
    identity: str
    from_revision: str
    to_revision: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        identity = self.identity

        from_revision = self.from_revision

        to_revision = self.to_revision

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
                "identity": identity,
                "from_revision": from_revision,
                "to_revision": to_revision,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        identity = d.pop("identity")

        from_revision = d.pop("from_revision")

        to_revision = d.pop("to_revision")

        knowledge_diff_request = cls(
            project_id=project_id,
            identity=identity,
            from_revision=from_revision,
            to_revision=to_revision,
        )

        knowledge_diff_request.additional_properties = d
        return knowledge_diff_request

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
