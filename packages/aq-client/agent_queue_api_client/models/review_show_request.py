from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReviewShowRequest")


@_attrs_define
class ReviewShowRequest:
    """
    Attributes:
        review_id (str):
        revision (int | None | Unset):
        comments (bool | None | Unset):
        diff_from (int | None | Unset):
    """

    review_id: str
    revision: int | None | Unset = UNSET
    comments: bool | None | Unset = UNSET
    diff_from: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        review_id = self.review_id

        revision: int | None | Unset
        if isinstance(self.revision, Unset):
            revision = UNSET
        else:
            revision = self.revision

        comments: bool | None | Unset
        if isinstance(self.comments, Unset):
            comments = UNSET
        else:
            comments = self.comments

        diff_from: int | None | Unset
        if isinstance(self.diff_from, Unset):
            diff_from = UNSET
        else:
            diff_from = self.diff_from

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review_id": review_id,
            }
        )
        if revision is not UNSET:
            field_dict["revision"] = revision
        if comments is not UNSET:
            field_dict["comments"] = comments
        if diff_from is not UNSET:
            field_dict["diff_from"] = diff_from

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        review_id = d.pop("review_id")

        def _parse_revision(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        revision = _parse_revision(d.pop("revision", UNSET))

        def _parse_comments(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        comments = _parse_comments(d.pop("comments", UNSET))

        def _parse_diff_from(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        diff_from = _parse_diff_from(d.pop("diff_from", UNSET))

        review_show_request = cls(
            review_id=review_id,
            revision=revision,
            comments=comments,
            diff_from=diff_from,
        )

        review_show_request.additional_properties = d
        return review_show_request

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
