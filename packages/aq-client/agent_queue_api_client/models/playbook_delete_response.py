from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookDeleteResponse")


@_attrs_define
class PlaybookDeleteResponse:
    """``playbook_delete``: ``deleted`` is False when no exact entry existed.

    Attributes:
        deleted (bool):
        success (bool | Unset):  Default: True.
        playbook_id (None | str | Unset):
        scope (None | str | Unset):
        scope_identifier (None | str | Unset):
    """

    deleted: bool
    success: bool | Unset = True
    playbook_id: None | str | Unset = UNSET
    scope: None | str | Unset = UNSET
    scope_identifier: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        deleted = self.deleted

        success = self.success

        playbook_id: None | str | Unset
        if isinstance(self.playbook_id, Unset):
            playbook_id = UNSET
        else:
            playbook_id = self.playbook_id

        scope: None | str | Unset
        if isinstance(self.scope, Unset):
            scope = UNSET
        else:
            scope = self.scope

        scope_identifier: None | str | Unset
        if isinstance(self.scope_identifier, Unset):
            scope_identifier = UNSET
        else:
            scope_identifier = self.scope_identifier

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "deleted": deleted,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if scope is not UNSET:
            field_dict["scope"] = scope
        if scope_identifier is not UNSET:
            field_dict["scope_identifier"] = scope_identifier

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        deleted = d.pop("deleted")

        success = d.pop("success", UNSET)

        def _parse_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_id = _parse_playbook_id(d.pop("playbook_id", UNSET))

        def _parse_scope(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scope = _parse_scope(d.pop("scope", UNSET))

        def _parse_scope_identifier(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scope_identifier = _parse_scope_identifier(d.pop("scope_identifier", UNSET))

        playbook_delete_response = cls(
            deleted=deleted,
            success=success,
            playbook_id=playbook_id,
            scope=scope,
            scope_identifier=scope_identifier,
        )

        playbook_delete_response.additional_properties = d
        return playbook_delete_response

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
