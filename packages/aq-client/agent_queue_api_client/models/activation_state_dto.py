from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.activation_state_dto_health import ActivationStateDTOHealth
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.activation_health_reason_dto import ActivationHealthReasonDTO


T = TypeVar("T", bound="ActivationStateDTO")


@_attrs_define
class ActivationStateDTO:
    """``enabled`` and ``health`` are independent (design spec).  A disabled
    activation still reports its computed health; ``health="disabled"`` is used
    only when there is no active artifact at all.

        Attributes:
            playbook_id (str):
            scope (str):
            scope_identifier (None | str | Unset):
            enabled (bool | Unset):  Default: False.
            active_artifact_sha256 (None | str | Unset):
            health (ActivationStateDTOHealth | Unset):  Default: ActivationStateDTOHealth.DISABLED.
            reasons (list[ActivationHealthReasonDTO] | Unset):
            activated_at (float | None | Unset):
            activated_by (None | str | Unset):
            pending_event_count (int | Unset):  Default: 0.
            running_count (int | Unset):  Default: 0.
            source_path (None | str | Unset):
            source_sha256 (None | str | Unset):
            active_source_sha256 (None | str | Unset):
            source_drift (bool | None | Unset):
            source_error (None | str | Unset):
    """

    playbook_id: str
    scope: str
    scope_identifier: None | str | Unset = UNSET
    enabled: bool | Unset = False
    active_artifact_sha256: None | str | Unset = UNSET
    health: ActivationStateDTOHealth | Unset = ActivationStateDTOHealth.DISABLED
    reasons: list[ActivationHealthReasonDTO] | Unset = UNSET
    activated_at: float | None | Unset = UNSET
    activated_by: None | str | Unset = UNSET
    pending_event_count: int | Unset = 0
    running_count: int | Unset = 0
    source_path: None | str | Unset = UNSET
    source_sha256: None | str | Unset = UNSET
    active_source_sha256: None | str | Unset = UNSET
    source_drift: bool | None | Unset = UNSET
    source_error: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        playbook_id = self.playbook_id

        scope = self.scope

        scope_identifier: None | str | Unset
        if isinstance(self.scope_identifier, Unset):
            scope_identifier = UNSET
        else:
            scope_identifier = self.scope_identifier

        enabled = self.enabled

        active_artifact_sha256: None | str | Unset
        if isinstance(self.active_artifact_sha256, Unset):
            active_artifact_sha256 = UNSET
        else:
            active_artifact_sha256 = self.active_artifact_sha256

        health: str | Unset = UNSET
        if not isinstance(self.health, Unset):
            health = self.health.value

        reasons: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.reasons, Unset):
            reasons = []
            for reasons_item_data in self.reasons:
                reasons_item = reasons_item_data.to_dict()
                reasons.append(reasons_item)

        activated_at: float | None | Unset
        if isinstance(self.activated_at, Unset):
            activated_at = UNSET
        else:
            activated_at = self.activated_at

        activated_by: None | str | Unset
        if isinstance(self.activated_by, Unset):
            activated_by = UNSET
        else:
            activated_by = self.activated_by

        pending_event_count = self.pending_event_count

        running_count = self.running_count

        source_path: None | str | Unset
        if isinstance(self.source_path, Unset):
            source_path = UNSET
        else:
            source_path = self.source_path

        source_sha256: None | str | Unset
        if isinstance(self.source_sha256, Unset):
            source_sha256 = UNSET
        else:
            source_sha256 = self.source_sha256

        active_source_sha256: None | str | Unset
        if isinstance(self.active_source_sha256, Unset):
            active_source_sha256 = UNSET
        else:
            active_source_sha256 = self.active_source_sha256

        source_drift: bool | None | Unset
        if isinstance(self.source_drift, Unset):
            source_drift = UNSET
        else:
            source_drift = self.source_drift

        source_error: None | str | Unset
        if isinstance(self.source_error, Unset):
            source_error = UNSET
        else:
            source_error = self.source_error

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "playbook_id": playbook_id,
                "scope": scope,
            }
        )
        if scope_identifier is not UNSET:
            field_dict["scope_identifier"] = scope_identifier
        if enabled is not UNSET:
            field_dict["enabled"] = enabled
        if active_artifact_sha256 is not UNSET:
            field_dict["active_artifact_sha256"] = active_artifact_sha256
        if health is not UNSET:
            field_dict["health"] = health
        if reasons is not UNSET:
            field_dict["reasons"] = reasons
        if activated_at is not UNSET:
            field_dict["activated_at"] = activated_at
        if activated_by is not UNSET:
            field_dict["activated_by"] = activated_by
        if pending_event_count is not UNSET:
            field_dict["pending_event_count"] = pending_event_count
        if running_count is not UNSET:
            field_dict["running_count"] = running_count
        if source_path is not UNSET:
            field_dict["source_path"] = source_path
        if source_sha256 is not UNSET:
            field_dict["source_sha256"] = source_sha256
        if active_source_sha256 is not UNSET:
            field_dict["active_source_sha256"] = active_source_sha256
        if source_drift is not UNSET:
            field_dict["source_drift"] = source_drift
        if source_error is not UNSET:
            field_dict["source_error"] = source_error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.activation_health_reason_dto import ActivationHealthReasonDTO

        d = dict(src_dict)
        playbook_id = d.pop("playbook_id")

        scope = d.pop("scope")

        def _parse_scope_identifier(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        scope_identifier = _parse_scope_identifier(d.pop("scope_identifier", UNSET))

        enabled = d.pop("enabled", UNSET)

        def _parse_active_artifact_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        active_artifact_sha256 = _parse_active_artifact_sha256(d.pop("active_artifact_sha256", UNSET))

        _health = d.pop("health", UNSET)
        health: ActivationStateDTOHealth | Unset
        if isinstance(_health, Unset):
            health = UNSET
        else:
            health = ActivationStateDTOHealth(_health)

        _reasons = d.pop("reasons", UNSET)
        reasons: list[ActivationHealthReasonDTO] | Unset = UNSET
        if _reasons is not UNSET:
            reasons = []
            for reasons_item_data in _reasons:
                reasons_item = ActivationHealthReasonDTO.from_dict(reasons_item_data)

                reasons.append(reasons_item)

        def _parse_activated_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        activated_at = _parse_activated_at(d.pop("activated_at", UNSET))

        def _parse_activated_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        activated_by = _parse_activated_by(d.pop("activated_by", UNSET))

        pending_event_count = d.pop("pending_event_count", UNSET)

        running_count = d.pop("running_count", UNSET)

        def _parse_source_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_path = _parse_source_path(d.pop("source_path", UNSET))

        def _parse_source_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_sha256 = _parse_source_sha256(d.pop("source_sha256", UNSET))

        def _parse_active_source_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        active_source_sha256 = _parse_active_source_sha256(d.pop("active_source_sha256", UNSET))

        def _parse_source_drift(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        source_drift = _parse_source_drift(d.pop("source_drift", UNSET))

        def _parse_source_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_error = _parse_source_error(d.pop("source_error", UNSET))

        activation_state_dto = cls(
            playbook_id=playbook_id,
            scope=scope,
            scope_identifier=scope_identifier,
            enabled=enabled,
            active_artifact_sha256=active_artifact_sha256,
            health=health,
            reasons=reasons,
            activated_at=activated_at,
            activated_by=activated_by,
            pending_event_count=pending_event_count,
            running_count=running_count,
            source_path=source_path,
            source_sha256=source_sha256,
            active_source_sha256=active_source_sha256,
            source_drift=source_drift,
            source_error=source_error,
        )

        return activation_state_dto
