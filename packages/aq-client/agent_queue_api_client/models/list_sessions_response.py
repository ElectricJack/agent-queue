from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.session_summary import SessionSummary


T = TypeVar("T", bound="ListSessionsResponse")


@_attrs_define
class ListSessionsResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        sessions (list[SessionSummary] | Unset):
        count (int | Unset):  Default: 0.
    """

    success: bool | Unset = True
    sessions: list[SessionSummary] | Unset = UNSET
    count: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        sessions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.sessions, Unset):
            sessions = []
            for sessions_item_data in self.sessions:
                sessions_item = sessions_item_data.to_dict()
                sessions.append(sessions_item)

        count = self.count

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if sessions is not UNSET:
            field_dict["sessions"] = sessions
        if count is not UNSET:
            field_dict["count"] = count

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.session_summary import SessionSummary  # noqa: PLC0415

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _sessions = d.pop("sessions", UNSET)
        sessions: list[SessionSummary] | Unset = UNSET
        if _sessions is not UNSET:
            sessions = []
            for sessions_item_data in _sessions:
                sessions_item = SessionSummary.from_dict(sessions_item_data)

                sessions.append(sessions_item)

        count = d.pop("count", UNSET)

        list_sessions_response = cls(
            success=success,
            sessions=sessions,
            count=count,
        )

        list_sessions_response.additional_properties = d
        return list_sessions_response

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
