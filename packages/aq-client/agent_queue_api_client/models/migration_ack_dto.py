from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define

T = TypeVar("T", bound="MigrationAckDTO")


@_attrs_define
class MigrationAckDTO:
    """
    Attributes:
        playbook_id (str):
        scope (str):
        scope_identifier (str):
        source_sha256 (str):
        reason (str):
        acknowledged_by (str):
        acknowledged_at (float):
    """

    playbook_id: str
    scope: str
    scope_identifier: str
    source_sha256: str
    reason: str
    acknowledged_by: str
    acknowledged_at: float

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        scope = self.scope

        scope_identifier = self.scope_identifier

        source_sha256 = self.source_sha256

        reason = self.reason

        acknowledged_by = self.acknowledged_by

        acknowledged_at = self.acknowledged_at

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "playbook_id": playbook_id,
                "scope": scope,
                "scope_identifier": scope_identifier,
                "source_sha256": source_sha256,
                "reason": reason,
                "acknowledged_by": acknowledged_by,
                "acknowledged_at": acknowledged_at,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        scope = d.pop("scope")

        scope_identifier = d.pop("scope_identifier")

        source_sha256 = d.pop("source_sha256")

        reason = d.pop("reason")

        acknowledged_by = d.pop("acknowledged_by")

        acknowledged_at = d.pop("acknowledged_at")

        migration_ack_dto = cls(
            playbook_id=playbook_id,
            scope=scope,
            scope_identifier=scope_identifier,
            source_sha256=source_sha256,
            reason=reason,
            acknowledged_by=acknowledged_by,
            acknowledged_at=acknowledged_at,
        )

        return migration_ack_dto
