from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="PlaybookDeleteRequest")


@_attrs_define
class PlaybookDeleteRequest:
    """
    Attributes:
        playbook_id (str):
        scope (str):
        scope_identifier (str):
        artifact_sha256 (str):
    """

    playbook_id: str
    scope: str
    scope_identifier: str
    artifact_sha256: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        scope = self.scope

        scope_identifier = self.scope_identifier

        artifact_sha256 = self.artifact_sha256

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "scope": scope,
                "scope_identifier": scope_identifier,
                "artifact_sha256": artifact_sha256,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        scope = d.pop("scope")

        scope_identifier = d.pop("scope_identifier")

        artifact_sha256 = d.pop("artifact_sha256")

        playbook_delete_request = cls(
            playbook_id=playbook_id,
            scope=scope,
            scope_identifier=scope_identifier,
            artifact_sha256=artifact_sha256,
        )

        playbook_delete_request.additional_properties = d
        return playbook_delete_request

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
