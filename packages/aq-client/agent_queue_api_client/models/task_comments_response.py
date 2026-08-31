from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_comment import TaskComment


T = TypeVar("T", bound="TaskCommentsResponse")


@_attrs_define
class TaskCommentsResponse:
    """
    Attributes:
        total (int):
        limit (int):
        offset (int):
        comments (list[TaskComment] | Unset):
    """

    total: int
    limit: int
    offset: int
    comments: list[TaskComment] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        total = self.total

        limit = self.limit

        offset = self.offset

        comments: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.comments, Unset):
            comments = []
            for comments_item_data in self.comments:
                comments_item = comments_item_data.to_dict()
                comments.append(comments_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )
        if comments is not UNSET:
            field_dict["comments"] = comments

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_comment import TaskComment  # noqa: PLC0415

        d = dict(src_dict)
        total = d.pop("total")

        limit = d.pop("limit")

        offset = d.pop("offset")

        _comments = d.pop("comments", UNSET)
        comments: list[TaskComment] | Unset = UNSET
        if _comments is not UNSET:
            comments = []
            for comments_item_data in _comments:
                comments_item = TaskComment.from_dict(comments_item_data)

                comments.append(comments_item)

        task_comments_response = cls(
            total=total,
            limit=limit,
            offset=offset,
            comments=comments,
        )

        task_comments_response.additional_properties = d
        return task_comments_response

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
