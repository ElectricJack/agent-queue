from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.session_summary import SessionSummary


T = TypeVar("T", bound="ShowSessionResponse")


@_attrs_define
class ShowSessionResponse:
    """
    Attributes:
        session (SessionSummary): One row of ``session_list`` output.

            ``idle_seconds`` and ``stalled`` are derived per-row in
            ``_cmd_session_list``; every other field mirrors ``sessions`` table
            columns via ``SessionCommandsMixin._session_dict``.
        success (bool | Unset):  Default: True.
    """

    session: SessionSummary
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        session = self.session.to_dict()

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "session": session,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.session_summary import SessionSummary

        d = dict(src_dict)
        session = SessionSummary.from_dict(d.pop("session"))

        success = d.pop("success", UNSET)

        show_session_response = cls(
            session=session,
            success=success,
        )

        show_session_response.additional_properties = d
        return show_session_response

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
