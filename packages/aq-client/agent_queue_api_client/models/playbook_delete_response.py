from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookDeleteResponse")


@_attrs_define
class PlaybookDeleteResponse:
    """Result of deleting one exact installed catalog entry.

    ``deleted=False`` alongside ``success=True`` is the idempotent answer: the
    entry the caller named was already gone.  Every refusal (an enabled
    activation, a stale hash, unfinished work, a policy reference) is a command
    error rather than a ``deleted=False`` body, so a client can treat this
    response as "the named entry is not installed any more".

        Attributes:
            success (bool | Unset):  Default: True.
            deleted (bool | Unset):  Default: False.
            playbook_id (None | str | Unset):
            scope (None | str | Unset):
            scope_identifier (None | str | Unset):
    """

    success: bool | Unset = True
    deleted: bool | Unset = False
    playbook_id: None | str | Unset = UNSET
    scope: None | str | Unset = UNSET
    scope_identifier: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        deleted = self.deleted

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

        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if deleted is not UNSET:
            field_dict["deleted"] = deleted
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
        success = d.pop("success", UNSET)

        deleted = d.pop("deleted", UNSET)

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
            success=success,
            deleted=deleted,
            playbook_id=playbook_id,
            scope=scope,
            scope_identifier=scope_identifier,
        )

        return playbook_delete_response
