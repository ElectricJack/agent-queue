from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookHealthRequest")


@_attrs_define
class PlaybookHealthRequest:
    """
    Attributes:
        playbook_id (None | str | Unset): Filter to a specific playbook ID. Omit for all playbooks.
        status (None | str | Unset): Filter by run status: running, paused, completed, failed, blocked, timed_out.
        limit (int | Unset): Max runs to analyse (default 200). Default: 200.
    """

    playbook_id: None | str | Unset = UNSET
    status: None | str | Unset = UNSET
    limit: int | Unset = 200
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id: None | str | Unset
        if isinstance(self.playbook_id, Unset):
            playbook_id = UNSET
        else:
            playbook_id = self.playbook_id

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        else:
            status = self.status

        limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if status is not UNSET:
            field_dict["status"] = status
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_id = _parse_playbook_id(d.pop("playbook_id", UNSET))

        def _parse_status(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        limit = d.pop("limit", UNSET)

        playbook_health_request = cls(
            playbook_id=playbook_id,
            status=status,
            limit=limit,
        )

        playbook_health_request.additional_properties = d
        return playbook_health_request

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
