from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.migration_inventory_entry_dto_disposition import MigrationInventoryEntryDTODisposition
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.artifact_ref_dto import ArtifactRefDTO
    from ..models.migration_reason_dto import MigrationReasonDTO
    from ..models.migration_source_ref_dto import MigrationSourceRefDTO


T = TypeVar("T", bound="MigrationInventoryEntryDTO")


@_attrs_define
class MigrationInventoryEntryDTO:
    """
    Attributes:
        playbook_id (str):
        scope (str):
        source (MigrationSourceRefDTO): Where an inventory entry's authoring Markdown lives.

            Distinct from :class:`SourceRefDTO`, which points at a span *inside* a
            source; this points at the whole file and carries its content hash.
        v1_kind (str):
        v1_enabled (bool):
        disposition (MigrationInventoryEntryDTODisposition):
        has_embedded_action_block (bool):
        scope_identifier (None | str | Unset):
        v1_version (int | None | Unset):
        reasons (list[MigrationReasonDTO] | Unset):
        artifact (ArtifactRefDTO | None | Unset):
        activation_health (None | str | Unset):
        acknowledged_by (None | str | Unset):
        acknowledged_at (float | None | Unset):
        pending_events (int | Unset):  Default: 0.
    """

    playbook_id: str
    scope: str
    source: MigrationSourceRefDTO
    v1_kind: str
    v1_enabled: bool
    disposition: MigrationInventoryEntryDTODisposition
    has_embedded_action_block: bool
    scope_identifier: None | str | Unset = UNSET
    v1_version: int | None | Unset = UNSET
    reasons: list[MigrationReasonDTO] | Unset = UNSET
    artifact: ArtifactRefDTO | None | Unset = UNSET
    activation_health: None | str | Unset = UNSET
    acknowledged_by: None | str | Unset = UNSET
    acknowledged_at: float | None | Unset = UNSET
    pending_events: int | Unset = 0

    def to_dict(self) -> dict[str, Any]:
        from ..models.artifact_ref_dto import ArtifactRefDTO

        playbook_id = self.playbook_id

        scope = self.scope

        source = self.source.to_dict()

        v1_kind = self.v1_kind

        v1_enabled = self.v1_enabled

        disposition = self.disposition.value

        has_embedded_action_block = self.has_embedded_action_block

        scope_identifier: None | str | Unset
        if isinstance(self.scope_identifier, Unset):
            scope_identifier = UNSET
        else:
            scope_identifier = self.scope_identifier

        v1_version: int | None | Unset
        if isinstance(self.v1_version, Unset):
            v1_version = UNSET
        else:
            v1_version = self.v1_version

        reasons: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.reasons, Unset):
            reasons = []
            for reasons_item_data in self.reasons:
                reasons_item = reasons_item_data.to_dict()
                reasons.append(reasons_item)

        artifact: dict[str, Any] | None | Unset
        if isinstance(self.artifact, Unset):
            artifact = UNSET
        elif isinstance(self.artifact, ArtifactRefDTO):
            artifact = self.artifact.to_dict()
        else:
            artifact = self.artifact

        activation_health: None | str | Unset
        if isinstance(self.activation_health, Unset):
            activation_health = UNSET
        else:
            activation_health = self.activation_health

        acknowledged_by: None | str | Unset
        if isinstance(self.acknowledged_by, Unset):
            acknowledged_by = UNSET
        else:
            acknowledged_by = self.acknowledged_by

        acknowledged_at: float | None | Unset
        if isinstance(self.acknowledged_at, Unset):
            acknowledged_at = UNSET
        else:
            acknowledged_at = self.acknowledged_at

        pending_events = self.pending_events

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "playbook_id": playbook_id,
                "scope": scope,
                "source": source,
                "v1_kind": v1_kind,
                "v1_enabled": v1_enabled,
                "disposition": disposition,
                "has_embedded_action_block": has_embedded_action_block,
            }
        )
        if scope_identifier is not UNSET:
            field_dict["scope_identifier"] = scope_identifier
        if v1_version is not UNSET:
            field_dict["v1_version"] = v1_version
        if reasons is not UNSET:
            field_dict["reasons"] = reasons
        if artifact is not UNSET:
            field_dict["artifact"] = artifact
        if activation_health is not UNSET:
            field_dict["activation_health"] = activation_health
        if acknowledged_by is not UNSET:
            field_dict["acknowledged_by"] = acknowledged_by
        if acknowledged_at is not UNSET:
            field_dict["acknowledged_at"] = acknowledged_at
        if pending_events is not UNSET:
            field_dict["pending_events"] = pending_events

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_ref_dto import ArtifactRefDTO
        from ..models.migration_reason_dto import MigrationReasonDTO
        from ..models.migration_source_ref_dto import MigrationSourceRefDTO

        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        scope = d.pop("scope")

        source = MigrationSourceRefDTO.from_dict(d.pop("source"))

        v1_kind = d.pop("v1_kind")

        v1_enabled = d.pop("v1_enabled")

        disposition = MigrationInventoryEntryDTODisposition(d.pop("disposition"))

        has_embedded_action_block = d.pop("has_embedded_action_block")

        def _parse_scope_identifier(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scope_identifier = _parse_scope_identifier(d.pop("scope_identifier", UNSET))

        def _parse_v1_version(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        v1_version = _parse_v1_version(d.pop("v1_version", UNSET))

        _reasons = d.pop("reasons", UNSET)
        reasons: list[MigrationReasonDTO] | Unset = UNSET
        if _reasons is not UNSET:
            reasons = []
            for reasons_item_data in _reasons:
                reasons_item = MigrationReasonDTO.from_dict(reasons_item_data)

                reasons.append(reasons_item)

        def _parse_artifact(data: object) -> ArtifactRefDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                artifact_type_0 = ArtifactRefDTO.from_dict(data)

                return artifact_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ArtifactRefDTO | None | Unset, data)

        artifact = _parse_artifact(d.pop("artifact", UNSET))

        def _parse_activation_health(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        activation_health = _parse_activation_health(d.pop("activation_health", UNSET))

        def _parse_acknowledged_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        acknowledged_by = _parse_acknowledged_by(d.pop("acknowledged_by", UNSET))

        def _parse_acknowledged_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        acknowledged_at = _parse_acknowledged_at(d.pop("acknowledged_at", UNSET))

        pending_events = d.pop("pending_events", UNSET)

        migration_inventory_entry_dto = cls(
            playbook_id=playbook_id,
            scope=scope,
            source=source,
            v1_kind=v1_kind,
            v1_enabled=v1_enabled,
            disposition=disposition,
            has_embedded_action_block=has_embedded_action_block,
            scope_identifier=scope_identifier,
            v1_version=v1_version,
            reasons=reasons,
            artifact=artifact,
            activation_health=activation_health,
            acknowledged_by=acknowledged_by,
            acknowledged_at=acknowledged_at,
            pending_events=pending_events,
        )

        return migration_inventory_entry_dto
