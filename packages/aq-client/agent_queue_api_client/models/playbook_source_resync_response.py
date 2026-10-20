from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="PlaybookSourceResyncResponse")


@_attrs_define
class PlaybookSourceResyncResponse:
    """
    Attributes:
        playbook_id (str):
        artifact_sha256 (str):
        source_hash (str):
        previous_source_hash (str):
        source_path (str):
        changed (bool):
        success (bool | Unset):  Default: True.
        backup_path (None | str | Unset):
    """

    playbook_id: str
    artifact_sha256: str
    source_hash: str
    previous_source_hash: str
    source_path: str
    changed: bool
    success: bool | Unset = True
    backup_path: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        artifact_sha256 = self.artifact_sha256

        source_hash = self.source_hash

        previous_source_hash = self.previous_source_hash

        source_path = self.source_path

        changed = self.changed

        success = self.success

        backup_path: None | str | Unset
        if isinstance(self.backup_path, Unset):
            backup_path = UNSET
        else:
            backup_path = self.backup_path

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "playbook_id": playbook_id,
                "artifact_sha256": artifact_sha256,
                "source_hash": source_hash,
                "previous_source_hash": previous_source_hash,
                "source_path": source_path,
                "changed": changed,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if backup_path is not UNSET:
            field_dict["backup_path"] = backup_path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        artifact_sha256 = d.pop("artifact_sha256")

        source_hash = d.pop("source_hash")

        previous_source_hash = d.pop("previous_source_hash")

        source_path = d.pop("source_path")

        changed = d.pop("changed")

        success = d.pop("success", UNSET)

        def _parse_backup_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        backup_path = _parse_backup_path(d.pop("backup_path", UNSET))

        playbook_source_resync_response = cls(
            playbook_id=playbook_id,
            artifact_sha256=artifact_sha256,
            source_hash=source_hash,
            previous_source_hash=previous_source_hash,
            source_path=source_path,
            changed=changed,
            success=success,
            backup_path=backup_path,
        )

        return playbook_source_resync_response
