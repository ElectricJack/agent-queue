from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.wait_source_redispatch_args_action import WaitSourceRedispatchArgsAction
from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitSourceRedispatchArgs")


@_attrs_define
class WaitSourceRedispatchArgs:
    """
    Attributes:
        project_id (str):
        action (WaitSourceRedispatchArgsAction | Unset):  Default: WaitSourceRedispatchArgsAction.REDISPATCH.
        source_seq (int | None | Unset):
        note (None | str | Unset):
        limit (int | Unset):  Default: 100.
    """

    project_id: str
    action: WaitSourceRedispatchArgsAction | Unset = WaitSourceRedispatchArgsAction.REDISPATCH
    source_seq: int | None | Unset = UNSET
    note: None | str | Unset = UNSET
    limit: int | Unset = 100

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        action: str | Unset = UNSET
        if not isinstance(self.action, Unset):
            action = self.action.value

        source_seq: int | None | Unset
        if isinstance(self.source_seq, Unset):
            source_seq = UNSET
        else:
            source_seq = self.source_seq

        note: None | str | Unset
        if isinstance(self.note, Unset):
            note = UNSET
        else:
            note = self.note

        limit = self.limit

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if action is not UNSET:
            field_dict["action"] = action
        if source_seq is not UNSET:
            field_dict["source_seq"] = source_seq
        if note is not UNSET:
            field_dict["note"] = note
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        _action = d.pop("action", UNSET)
        action: WaitSourceRedispatchArgsAction | Unset
        if isinstance(_action, Unset):
            action = UNSET
        else:
            action = WaitSourceRedispatchArgsAction(_action)

        def _parse_source_seq(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        source_seq = _parse_source_seq(d.pop("source_seq", UNSET))

        def _parse_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        note = _parse_note(d.pop("note", UNSET))

        limit = d.pop("limit", UNSET)

        wait_source_redispatch_args = cls(
            project_id=project_id,
            action=action,
            source_seq=source_seq,
            note=note,
            limit=limit,
        )

        return wait_source_redispatch_args
