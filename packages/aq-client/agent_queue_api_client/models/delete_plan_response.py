from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeletePlanResponse")


@_attrs_define
class DeletePlanResponse:
    """
    Attributes:
        deleted (str):
        title (str):
        status (str | Unset):  Default: 'COMPLETED'.
        draft_subtasks_deleted (int | Unset):  Default: 0.
    """

    deleted: str
    title: str
    status: str | Unset = "COMPLETED"
    draft_subtasks_deleted: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        deleted = self.deleted

        title = self.title

        status = self.status

        draft_subtasks_deleted = self.draft_subtasks_deleted

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "deleted": deleted,
                "title": title,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if draft_subtasks_deleted is not UNSET:
            field_dict["draft_subtasks_deleted"] = draft_subtasks_deleted

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        deleted = d.pop("deleted")

        title = d.pop("title")

        status = d.pop("status", UNSET)

        draft_subtasks_deleted = d.pop("draft_subtasks_deleted", UNSET)

        delete_plan_response = cls(
            deleted=deleted,
            title=title,
            status=status,
            draft_subtasks_deleted=draft_subtasks_deleted,
        )

        delete_plan_response.additional_properties = d
        return delete_plan_response

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
