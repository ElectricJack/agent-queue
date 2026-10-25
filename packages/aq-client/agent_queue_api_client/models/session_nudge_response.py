from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SessionNudgeResponse")


@_attrs_define
class SessionNudgeResponse:
    """
    Attributes:
        success (bool):
        session_id (str):
        delivered (bool | Unset):  Default: False.
        error (None | str | Unset):
        state (None | str | Unset):
        composer_dirty (bool | None | Unset):
        pending_action (None | str | Unset):
        reason (None | str | Unset):
    """

    success: bool
    session_id: str
    delivered: bool | Unset = False
    error: None | str | Unset = UNSET
    state: None | str | Unset = UNSET
    composer_dirty: bool | None | Unset = UNSET
    pending_action: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        session_id = self.session_id

        delivered = self.delivered

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        composer_dirty: bool | None | Unset
        if isinstance(self.composer_dirty, Unset):
            composer_dirty = UNSET
        else:
            composer_dirty = self.composer_dirty

        pending_action: None | str | Unset
        if isinstance(self.pending_action, Unset):
            pending_action = UNSET
        else:
            pending_action = self.pending_action

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "success": success,
                "session_id": session_id,
            }
        )
        if delivered is not UNSET:
            field_dict["delivered"] = delivered
        if error is not UNSET:
            field_dict["error"] = error
        if state is not UNSET:
            field_dict["state"] = state
        if composer_dirty is not UNSET:
            field_dict["composer_dirty"] = composer_dirty
        if pending_action is not UNSET:
            field_dict["pending_action"] = pending_action
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        success = d.pop("success")

        session_id = d.pop("session_id")

        delivered = d.pop("delivered", UNSET)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        def _parse_composer_dirty(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        composer_dirty = _parse_composer_dirty(d.pop("composer_dirty", UNSET))

        def _parse_pending_action(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pending_action = _parse_pending_action(d.pop("pending_action", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        session_nudge_response = cls(
            success=success,
            session_id=session_id,
            delivered=delivered,
            error=error,
            state=state,
            composer_dirty=composer_dirty,
            pending_action=pending_action,
            reason=reason,
        )

        session_nudge_response.additional_properties = d
        return session_nudge_response

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
