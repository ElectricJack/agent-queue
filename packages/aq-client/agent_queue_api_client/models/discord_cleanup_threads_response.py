from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DiscordCleanupThreadsResponse")


@_attrs_define
class DiscordCleanupThreadsResponse:
    """Result of ``discord_cleanup_threads``.

    ``skipped_live`` is reported rather than inferred: the useful question
    after a cleanup is "what did it leave alone, and why", and the answer is
    threads whose task is still running.

        Attributes:
            channel (str):
            success (bool | Unset):  Default: True.
            dry_run (bool | Unset):  Default: False.
            mode (None | str | Unset):
            threads_found (int | None | Unset):
            would_archive (int | None | Unset):
            would_delete (int | None | Unset):
            archived (int | None | Unset):
            deleted (int | None | Unset):
            failed (int | None | Unset):
            skipped_live (int | Unset):  Default: 0.
            note (None | str | Unset):
            warning (None | str | Unset):
    """

    channel: str
    success: bool | Unset = True
    dry_run: bool | Unset = False
    mode: None | str | Unset = UNSET
    threads_found: int | None | Unset = UNSET
    would_archive: int | None | Unset = UNSET
    would_delete: int | None | Unset = UNSET
    archived: int | None | Unset = UNSET
    deleted: int | None | Unset = UNSET
    failed: int | None | Unset = UNSET
    skipped_live: int | Unset = 0
    note: None | str | Unset = UNSET
    warning: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        channel = self.channel

        success = self.success

        dry_run = self.dry_run

        mode: None | str | Unset
        if isinstance(self.mode, Unset):
            mode = UNSET
        else:
            mode = self.mode

        threads_found: int | None | Unset
        if isinstance(self.threads_found, Unset):
            threads_found = UNSET
        else:
            threads_found = self.threads_found

        would_archive: int | None | Unset
        if isinstance(self.would_archive, Unset):
            would_archive = UNSET
        else:
            would_archive = self.would_archive

        would_delete: int | None | Unset
        if isinstance(self.would_delete, Unset):
            would_delete = UNSET
        else:
            would_delete = self.would_delete

        archived: int | None | Unset
        if isinstance(self.archived, Unset):
            archived = UNSET
        else:
            archived = self.archived

        deleted: int | None | Unset
        if isinstance(self.deleted, Unset):
            deleted = UNSET
        else:
            deleted = self.deleted

        failed: int | None | Unset
        if isinstance(self.failed, Unset):
            failed = UNSET
        else:
            failed = self.failed

        skipped_live = self.skipped_live

        note: None | str | Unset
        if isinstance(self.note, Unset):
            note = UNSET
        else:
            note = self.note

        warning: None | str | Unset
        if isinstance(self.warning, Unset):
            warning = UNSET
        else:
            warning = self.warning

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
        if mode is not UNSET:
            field_dict["mode"] = mode
        if threads_found is not UNSET:
            field_dict["threads_found"] = threads_found
        if would_archive is not UNSET:
            field_dict["would_archive"] = would_archive
        if would_delete is not UNSET:
            field_dict["would_delete"] = would_delete
        if archived is not UNSET:
            field_dict["archived"] = archived
        if deleted is not UNSET:
            field_dict["deleted"] = deleted
        if failed is not UNSET:
            field_dict["failed"] = failed
        if skipped_live is not UNSET:
            field_dict["skipped_live"] = skipped_live
        if note is not UNSET:
            field_dict["note"] = note
        if warning is not UNSET:
            field_dict["warning"] = warning

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        channel = d.pop("channel")

        success = d.pop("success", UNSET)

        dry_run = d.pop("dry_run", UNSET)

        def _parse_mode(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        mode = _parse_mode(d.pop("mode", UNSET))

        def _parse_threads_found(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        threads_found = _parse_threads_found(d.pop("threads_found", UNSET))

        def _parse_would_archive(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        would_archive = _parse_would_archive(d.pop("would_archive", UNSET))

        def _parse_would_delete(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        would_delete = _parse_would_delete(d.pop("would_delete", UNSET))

        def _parse_archived(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        archived = _parse_archived(d.pop("archived", UNSET))

        def _parse_deleted(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        deleted = _parse_deleted(d.pop("deleted", UNSET))

        def _parse_failed(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        failed = _parse_failed(d.pop("failed", UNSET))

        skipped_live = d.pop("skipped_live", UNSET)

        def _parse_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        note = _parse_note(d.pop("note", UNSET))

        def _parse_warning(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        warning = _parse_warning(d.pop("warning", UNSET))

        discord_cleanup_threads_response = cls(
            channel=channel,
            success=success,
            dry_run=dry_run,
            mode=mode,
            threads_found=threads_found,
            would_archive=would_archive,
            would_delete=would_delete,
            archived=archived,
            deleted=deleted,
            failed=failed,
            skipped_live=skipped_live,
            note=note,
            warning=warning,
        )

        discord_cleanup_threads_response.additional_properties = d
        return discord_cleanup_threads_response

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
