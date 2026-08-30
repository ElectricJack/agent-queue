from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="CreatePlaybookResponse")


@_attrs_define
class CreatePlaybookResponse:
    """
    Attributes:
        playbook_id (str):
        path (str):
        source_hash (str):
        created (bool | Unset):  Default: True.
    """

    playbook_id: str
    path: str
    source_hash: str
    created: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        path = self.path

        source_hash = self.source_hash

        created = self.created

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
                "path": path,
                "source_hash": source_hash,
            }
        )
        if created is not UNSET:
            field_dict["created"] = created

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        path = d.pop("path")

        source_hash = d.pop("source_hash")

        created = d.pop("created", UNSET)

        create_playbook_response = cls(
            playbook_id=playbook_id,
            path=path,
            source_hash=source_hash,
            created=created,
        )

        create_playbook_response.additional_properties = d
        return create_playbook_response

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
