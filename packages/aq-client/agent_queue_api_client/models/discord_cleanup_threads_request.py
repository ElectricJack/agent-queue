from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DiscordCleanupThreadsRequest")


@_attrs_define
class DiscordCleanupThreadsRequest:
    """
    Attributes:
        channel_id (None | str | Unset): Target channel id.
        project_id (None | str | Unset): Use this project's channel instead of channel_id.
        mode (None | str | Unset): archive (reversible, default) or delete (permanent).
        only_closed (bool | None | Unset): Only touch threads whose task is finished (default true). Matched via
            tasks.discord_thread_id.
        limit (int | None | Unset): How many archived threads to scan (default 500).
        confirm (bool | None | Unset): Actually apply. Without it this is a dry run.
    """

    channel_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    mode: None | str | Unset = UNSET
    only_closed: bool | None | Unset = UNSET
    limit: int | None | Unset = UNSET
    confirm: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        channel_id: None | str | Unset
        if isinstance(self.channel_id, Unset):
            channel_id = UNSET
        else:
            channel_id = self.channel_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        mode: None | str | Unset
        if isinstance(self.mode, Unset):
            mode = UNSET
        else:
            mode = self.mode

        only_closed: bool | None | Unset
        if isinstance(self.only_closed, Unset):
            only_closed = UNSET
        else:
            only_closed = self.only_closed

        limit: int | None | Unset
        if isinstance(self.limit, Unset):
            limit = UNSET
        else:
            limit = self.limit

        confirm: bool | None | Unset
        if isinstance(self.confirm, Unset):
            confirm = UNSET
        else:
            confirm = self.confirm

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if channel_id is not UNSET:
            field_dict["channel_id"] = channel_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if mode is not UNSET:
            field_dict["mode"] = mode
        if only_closed is not UNSET:
            field_dict["only_closed"] = only_closed
        if limit is not UNSET:
            field_dict["limit"] = limit
        if confirm is not UNSET:
            field_dict["confirm"] = confirm

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_channel_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        channel_id = _parse_channel_id(d.pop("channel_id", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        mode = _parse_mode(d.pop("mode", UNSET))

        def _parse_only_closed(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        only_closed = _parse_only_closed(d.pop("only_closed", UNSET))

        def _parse_limit(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        limit = _parse_limit(d.pop("limit", UNSET))

        def _parse_confirm(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        confirm = _parse_confirm(d.pop("confirm", UNSET))

        discord_cleanup_threads_request = cls(
            channel_id=channel_id,
            project_id=project_id,
            mode=mode,
            only_closed=only_closed,
            limit=limit,
            confirm=confirm,
        )

        discord_cleanup_threads_request.additional_properties = d
        return discord_cleanup_threads_request

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
