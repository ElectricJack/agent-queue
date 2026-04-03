from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="PromoteNoteResponse")


@_attrs_define
class PromoteNoteResponse:
    """
    Attributes:
        project_id (str):
        note (None | str | Unset):
        status (str | Unset):  Default: ''.
        message (str | Unset):  Default: ''.
        profile_preview (None | str | Unset):
    """

    project_id: str
    note: None | str | Unset = UNSET
    status: str | Unset = ""
    message: str | Unset = ""
    profile_preview: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        note: None | str | Unset
        if isinstance(self.note, Unset):
            note = UNSET
        else:
            note = self.note

        status = self.status

        message = self.message

        profile_preview: None | str | Unset
        if isinstance(self.profile_preview, Unset):
            profile_preview = UNSET
        else:
            profile_preview = self.profile_preview

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if note is not UNSET:
            field_dict["note"] = note
        if status is not UNSET:
            field_dict["status"] = status
        if message is not UNSET:
            field_dict["message"] = message
        if profile_preview is not UNSET:
            field_dict["profile_preview"] = profile_preview

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        note = _parse_note(d.pop("note", UNSET))

        status = d.pop("status", UNSET)

        message = d.pop("message", UNSET)

        def _parse_profile_preview(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        profile_preview = _parse_profile_preview(d.pop("profile_preview", UNSET))

        promote_note_response = cls(
            project_id=project_id,
            note=note,
            status=status,
            message=message,
            profile_preview=profile_preview,
        )

        promote_note_response.additional_properties = d
        return promote_note_response

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
