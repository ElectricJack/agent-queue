from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="SessionDesiredStateResponse")


@_attrs_define
class SessionDesiredStateResponse:
    """``session_sleep`` / ``session_wake`` — intent, not observation.

    Both fields are returned so a caller can see the gap it just opened:
    ``desired_state`` is what was written, ``state`` is what the runtime
    still shows until the reconciler converges.

        Attributes:
            session_id (str):
            desired_state (str):
            success (bool | Unset):  Default: True.
            state (None | str | Unset):
    """

    session_id: str
    desired_state: str
    success: bool | Unset = True
    state: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        session_id = self.session_id

        desired_state = self.desired_state

        success = self.success

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "session_id": session_id,
                "desired_state": desired_state,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if state is not UNSET:
            field_dict["state"] = state

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        session_id = d.pop("session_id")

        desired_state = d.pop("desired_state")

        success = d.pop("success", UNSET)

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        session_desired_state_response = cls(
            session_id=session_id,
            desired_state=desired_state,
            success=success,
            state=state,
        )

        session_desired_state_response.additional_properties = d
        return session_desired_state_response

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
