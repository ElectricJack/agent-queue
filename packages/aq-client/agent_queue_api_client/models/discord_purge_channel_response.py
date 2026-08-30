from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DiscordPurgeChannelResponse")


@_attrs_define
class DiscordPurgeChannelResponse:
    """Result of ``discord_purge_channel``.

    Both shapes share one model because the command is a dry run by default:
    without ``confirm`` it reports ``deletable`` and sets ``dry_run``, and with
    it reports ``deleted``.  ``too_old_to_bulk_delete`` is always present —
    Discord refuses to bulk-delete messages over 14 days old, and a purge that
    reported success while silently leaving hundreds behind would be worse than
    one that says what it could not reach.

        Attributes:
            channel (str):
            success (bool | Unset):  Default: True.
            dry_run (bool | Unset):  Default: False.
            deletable (int | None | Unset):
            deleted (int | None | Unset):
            too_old_to_bulk_delete (int | Unset):  Default: 0.
            note (None | str | Unset):
    """

    channel: str
    success: bool | Unset = True
    dry_run: bool | Unset = False
    deletable: int | None | Unset = UNSET
    deleted: int | None | Unset = UNSET
    too_old_to_bulk_delete: int | Unset = 0
    note: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        channel = self.channel

        success = self.success

        dry_run = self.dry_run

        deletable: int | None | Unset
        if isinstance(self.deletable, Unset):
            deletable = UNSET
        else:
            deletable = self.deletable

        deleted: int | None | Unset
        if isinstance(self.deleted, Unset):
            deleted = UNSET
        else:
            deleted = self.deleted

        too_old_to_bulk_delete = self.too_old_to_bulk_delete

        note: None | str | Unset
        if isinstance(self.note, Unset):
            note = UNSET
        else:
            note = self.note

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "channel": channel,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if deletable is not UNSET:
            field_dict["deletable"] = deletable
        if deleted is not UNSET:
            field_dict["deleted"] = deleted
        if too_old_to_bulk_delete is not UNSET:
            field_dict["too_old_to_bulk_delete"] = too_old_to_bulk_delete
        if note is not UNSET:
            field_dict["note"] = note

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        channel = d.pop("channel")

        success = d.pop("success", UNSET)

        dry_run = d.pop("dry_run", UNSET)

        def _parse_deletable(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        deletable = _parse_deletable(d.pop("deletable", UNSET))

        def _parse_deleted(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        deleted = _parse_deleted(d.pop("deleted", UNSET))

        too_old_to_bulk_delete = d.pop("too_old_to_bulk_delete", UNSET)

        def _parse_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        note = _parse_note(d.pop("note", UNSET))

        discord_purge_channel_response = cls(
            channel=channel,
            success=success,
            dry_run=dry_run,
            deletable=deletable,
            deleted=deleted,
            too_old_to_bulk_delete=too_old_to_bulk_delete,
            note=note,
        )

        discord_purge_channel_response.additional_properties = d
        return discord_purge_channel_response

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
