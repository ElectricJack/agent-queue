from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeletePlaybookResponse")


@_attrs_define
class DeletePlaybookResponse:
    """
    Attributes:
        playbook_id (str):
        deleted (bool | Unset):  Default: True.
        archived_path (None | str | Unset):
        removed_from_registry (bool | Unset):  Default: False.
    """

    playbook_id: str
    deleted: bool | Unset = True
    archived_path: None | str | Unset = UNSET
    removed_from_registry: bool | Unset = False
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        deleted = self.deleted

        archived_path: None | str | Unset
        if isinstance(self.archived_path, Unset):
            archived_path = UNSET
        else:
            archived_path = self.archived_path

        removed_from_registry = self.removed_from_registry

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "playbook_id": playbook_id,
            }
        )
        if deleted is not UNSET:
            field_dict["deleted"] = deleted
        if archived_path is not UNSET:
            field_dict["archived_path"] = archived_path
        if removed_from_registry is not UNSET:
            field_dict["removed_from_registry"] = removed_from_registry

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        deleted = d.pop("deleted", UNSET)

        def _parse_archived_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        archived_path = _parse_archived_path(d.pop("archived_path", UNSET))

        removed_from_registry = d.pop("removed_from_registry", UNSET)

        delete_playbook_response = cls(
            playbook_id=playbook_id,
            deleted=deleted,
            archived_path=archived_path,
            removed_from_registry=removed_from_registry,
        )

        delete_playbook_response.additional_properties = d
        return delete_playbook_response

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
