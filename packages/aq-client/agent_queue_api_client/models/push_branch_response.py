from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.commit_author_note import CommitAuthorNote


T = TypeVar("T", bound="PushBranchResponse")


@_attrs_define
class PushBranchResponse:
    """
    Attributes:
        project_id (str):
        branch (str | Unset):  Default: ''.
        status (str | Unset):  Default: ''.
        oid (None | str | Unset):
        identity_notes (list[CommitAuthorNote] | Unset):
        identity_warning (None | str | Unset):
    """

    project_id: str
    branch: str | Unset = ""
    status: str | Unset = ""
    oid: None | str | Unset = UNSET
    identity_notes: list[CommitAuthorNote] | Unset = UNSET
    identity_warning: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        branch = self.branch

        status = self.status

        oid: None | str | Unset
        if isinstance(self.oid, Unset):
            oid = UNSET
        else:
            oid = self.oid

        identity_notes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.identity_notes, Unset):
            identity_notes = []
            for identity_notes_item_data in self.identity_notes:
                identity_notes_item = identity_notes_item_data.to_dict()
                identity_notes.append(identity_notes_item)

        identity_warning: None | str | Unset
        if isinstance(self.identity_warning, Unset):
            identity_warning = UNSET
        else:
            identity_warning = self.identity_warning

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if branch is not UNSET:
            field_dict["branch"] = branch
        if status is not UNSET:
            field_dict["status"] = status
        if oid is not UNSET:
            field_dict["oid"] = oid
        if identity_notes is not UNSET:
            field_dict["identity_notes"] = identity_notes
        if identity_warning is not UNSET:
            field_dict["identity_warning"] = identity_warning

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.commit_author_note import CommitAuthorNote

        d = dict(src_dict)
        project_id = d.pop("project_id")

        branch = d.pop("branch", UNSET)

        status = d.pop("status", UNSET)

        def _parse_oid(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        oid = _parse_oid(d.pop("oid", UNSET))

        _identity_notes = d.pop("identity_notes", UNSET)
        identity_notes: list[CommitAuthorNote] | Unset = UNSET
        if _identity_notes is not UNSET:
            identity_notes = []
            for identity_notes_item_data in _identity_notes:
                identity_notes_item = CommitAuthorNote.from_dict(identity_notes_item_data)

                identity_notes.append(identity_notes_item)

        def _parse_identity_warning(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        identity_warning = _parse_identity_warning(d.pop("identity_warning", UNSET))

        push_branch_response = cls(
            project_id=project_id,
            branch=branch,
            status=status,
            oid=oid,
            identity_notes=identity_notes,
            identity_warning=identity_warning,
        )

        push_branch_response.additional_properties = d
        return push_branch_response

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
