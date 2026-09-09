from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.deleted_branch import DeletedBranch


T = TypeVar("T", bound="DeleteTaskResponse")


@_attrs_define
class DeleteTaskResponse:
    """
    Attributes:
        deleted (str):
        title (str):
        discarded_branches (list[DeletedBranch] | Unset):
    """

    deleted: str
    title: str
    discarded_branches: list[DeletedBranch] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        deleted = self.deleted

        title = self.title

        discarded_branches: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.discarded_branches, Unset):
            discarded_branches = []
            for discarded_branches_item_data in self.discarded_branches:
                discarded_branches_item = discarded_branches_item_data.to_dict()
                discarded_branches.append(discarded_branches_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "deleted": deleted,
                "title": title,
            }
        )
        if discarded_branches is not UNSET:
            field_dict["discarded_branches"] = discarded_branches

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.deleted_branch import DeletedBranch

        d = dict(src_dict)
        deleted = d.pop("deleted")

        title = d.pop("title")

        _discarded_branches = d.pop("discarded_branches", UNSET)
        discarded_branches: list[DeletedBranch] | Unset = UNSET
        if _discarded_branches is not UNSET:
            discarded_branches = []
            for discarded_branches_item_data in _discarded_branches:
                discarded_branches_item = DeletedBranch.from_dict(discarded_branches_item_data)

                discarded_branches.append(discarded_branches_item)

        delete_task_response = cls(
            deleted=deleted,
            title=title,
            discarded_branches=discarded_branches,
        )

        delete_task_response.additional_properties = d
        return delete_task_response

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
