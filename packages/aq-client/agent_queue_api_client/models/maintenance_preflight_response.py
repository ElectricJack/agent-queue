from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.maintenance_session import MaintenanceSession


T = TypeVar("T", bound="MaintenancePreflightResponse")


@_attrs_define
class MaintenancePreflightResponse:
    """Admission after authenticating, inventorying sessions, and recording an audit.

    Attributes:
        success (bool):
        actor (None | str | Unset):
        audit_id (int | None | Unset):
        sessions (list[MaintenanceSession] | None | Unset):
        error (None | str | Unset):
    """

    success: bool
    actor: None | str | Unset = UNSET
    audit_id: int | None | Unset = UNSET
    sessions: list[MaintenanceSession] | None | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        actor: None | str | Unset
        if isinstance(self.actor, Unset):
            actor = UNSET
        else:
            actor = self.actor

        audit_id: int | None | Unset
        if isinstance(self.audit_id, Unset):
            audit_id = UNSET
        else:
            audit_id = self.audit_id

        sessions: list[dict[str, Any]] | None | Unset
        if isinstance(self.sessions, Unset):
            sessions = UNSET
        elif isinstance(self.sessions, list):
            sessions = []
            for sessions_type_0_item_data in self.sessions:
                sessions_type_0_item = sessions_type_0_item_data.to_dict()
                sessions.append(sessions_type_0_item)

        else:
            sessions = self.sessions

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
            }
        )
        if actor is not UNSET:
            field_dict["actor"] = actor
        if audit_id is not UNSET:
            field_dict["audit_id"] = audit_id
        if sessions is not UNSET:
            field_dict["sessions"] = sessions
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.maintenance_session import MaintenanceSession

        d = dict(src_dict)
        success = d.pop("success")

        def _parse_actor(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        actor = _parse_actor(d.pop("actor", UNSET))

        def _parse_audit_id(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        audit_id = _parse_audit_id(d.pop("audit_id", UNSET))

        def _parse_sessions(data: object) -> list[MaintenanceSession] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                sessions_type_0 = []
                _sessions_type_0 = data
                for sessions_type_0_item_data in _sessions_type_0:
                    sessions_type_0_item = MaintenanceSession.from_dict(sessions_type_0_item_data)

                    sessions_type_0.append(sessions_type_0_item)

                return sessions_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[MaintenanceSession] | None | Unset, data)

        sessions = _parse_sessions(d.pop("sessions", UNSET))

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        maintenance_preflight_response = cls(
            success=success,
            actor=actor,
            audit_id=audit_id,
            sessions=sessions,
            error=error,
        )

        maintenance_preflight_response.additional_properties = d
        return maintenance_preflight_response

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
