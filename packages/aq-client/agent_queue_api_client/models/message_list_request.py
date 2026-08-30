from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MessageListRequest")


@_attrs_define
class MessageListRequest:
    """
    Attributes:
        project_id (None | str | Unset): Filter by project
        thread_id (None | str | Unset): Filter by conversation thread
        to_kind (None | str | Unset): Filter by recipient kind
        to_id (None | str | Unset): Filter by recipient id
        include_archived (bool | Unset): Include archived rows Default: False.
        since (float | None | Unset): Only messages created after this epoch timestamp
        limit (int | Unset): Max rows (default 100) Default: 100.
    """

    project_id: None | str | Unset = UNSET
    thread_id: None | str | Unset = UNSET
    to_kind: None | str | Unset = UNSET
    to_id: None | str | Unset = UNSET
    include_archived: bool | Unset = False
    since: float | None | Unset = UNSET
    limit: int | Unset = 100
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        thread_id: None | str | Unset
        if isinstance(self.thread_id, Unset):
            thread_id = UNSET
        else:
            thread_id = self.thread_id

        to_kind: None | str | Unset
        if isinstance(self.to_kind, Unset):
            to_kind = UNSET
        else:
            to_kind = self.to_kind

        to_id: None | str | Unset
        if isinstance(self.to_id, Unset):
            to_id = UNSET
        else:
            to_id = self.to_id

        include_archived = self.include_archived

        since: float | None | Unset
        if isinstance(self.since, Unset):
            since = UNSET
        else:
            since = self.since

        limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if thread_id is not UNSET:
            field_dict["thread_id"] = thread_id
        if to_kind is not UNSET:
            field_dict["to_kind"] = to_kind
        if to_id is not UNSET:
            field_dict["to_id"] = to_id
        if include_archived is not UNSET:
            field_dict["include_archived"] = include_archived
        if since is not UNSET:
            field_dict["since"] = since
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_thread_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        thread_id = _parse_thread_id(d.pop("thread_id", UNSET))

        def _parse_to_kind(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to_kind = _parse_to_kind(d.pop("to_kind", UNSET))

        def _parse_to_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        to_id = _parse_to_id(d.pop("to_id", UNSET))

        include_archived = d.pop("include_archived", UNSET)

        def _parse_since(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        since = _parse_since(d.pop("since", UNSET))

        limit = d.pop("limit", UNSET)

        message_list_request = cls(
            project_id=project_id,
            thread_id=thread_id,
            to_kind=to_kind,
            to_id=to_id,
            include_archived=include_archived,
            since=since,
            limit=limit,
        )

        message_list_request.additional_properties = d
        return message_list_request

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
