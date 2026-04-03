from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.archive_tasks_response_archived_item import ArchiveTasksResponseArchivedItem


T = TypeVar("T", bound="ArchiveTasksResponse")


@_attrs_define
class ArchiveTasksResponse:
    """
    Attributes:
        archived_count (int | Unset):  Default: 0.
        archived_ids (list[str] | Unset):
        archived (list[ArchiveTasksResponseArchivedItem] | Unset):
        archive_dir (None | str | Unset):
        project_id (None | str | Unset):
    """

    archived_count: int | Unset = 0
    archived_ids: list[str] | Unset = UNSET
    archived: list[ArchiveTasksResponseArchivedItem] | Unset = UNSET
    archive_dir: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        archived_count = self.archived_count

        archived_ids: list[str] | Unset = UNSET
        if not isinstance(self.archived_ids, Unset):
            archived_ids = self.archived_ids

        archived: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.archived, Unset):
            archived = []
            for archived_item_data in self.archived:
                archived_item = archived_item_data.to_dict()
                archived.append(archived_item)

        archive_dir: None | str | Unset
        if isinstance(self.archive_dir, Unset):
            archive_dir = UNSET
        else:
            archive_dir = self.archive_dir

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if archived_count is not UNSET:
            field_dict["archived_count"] = archived_count
        if archived_ids is not UNSET:
            field_dict["archived_ids"] = archived_ids
        if archived is not UNSET:
            field_dict["archived"] = archived
        if archive_dir is not UNSET:
            field_dict["archive_dir"] = archive_dir
        if project_id is not UNSET:
            field_dict["project_id"] = project_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.archive_tasks_response_archived_item import ArchiveTasksResponseArchivedItem

        d = dict(src_dict)
        archived_count = d.pop("archived_count", UNSET)

        archived_ids = cast(list[str], d.pop("archived_ids", UNSET))

        _archived = d.pop("archived", UNSET)
        archived: list[ArchiveTasksResponseArchivedItem] | Unset = UNSET
        if _archived is not UNSET:
            archived = []
            for archived_item_data in _archived:
                archived_item = ArchiveTasksResponseArchivedItem.from_dict(archived_item_data)

                archived.append(archived_item)

        def _parse_archive_dir(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        archive_dir = _parse_archive_dir(d.pop("archive_dir", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        archive_tasks_response = cls(
            archived_count=archived_count,
            archived_ids=archived_ids,
            archived=archived,
            archive_dir=archive_dir,
            project_id=project_id,
        )

        archive_tasks_response.additional_properties = d
        return archive_tasks_response

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
