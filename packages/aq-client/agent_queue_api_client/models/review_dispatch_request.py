from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReviewDispatchRequest")


@_attrs_define
class ReviewDispatchRequest:
    """
    Attributes:
        review_id (str):
        count (int | None | Unset): Number of reviewer tasks (default 1).
        intelligence_class (None | str | Unset): Class hint for the reviewers (default deep-high).
        revision (int | None | Unset):
        with_comments (bool | None | Unset):
        focus (None | str | Unset):
        force (bool | None | Unset):
    """

    review_id: str
    count: int | None | Unset = UNSET
    intelligence_class: None | str | Unset = UNSET
    revision: int | None | Unset = UNSET
    with_comments: bool | None | Unset = UNSET
    focus: None | str | Unset = UNSET
    force: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        review_id = self.review_id

        count: int | None | Unset
        if isinstance(self.count, Unset):
            count = UNSET
        else:
            count = self.count

        intelligence_class: None | str | Unset
        if isinstance(self.intelligence_class, Unset):
            intelligence_class = UNSET
        else:
            intelligence_class = self.intelligence_class

        revision: int | None | Unset
        if isinstance(self.revision, Unset):
            revision = UNSET
        else:
            revision = self.revision

        with_comments: bool | None | Unset
        if isinstance(self.with_comments, Unset):
            with_comments = UNSET
        else:
            with_comments = self.with_comments

        focus: None | str | Unset
        if isinstance(self.focus, Unset):
            focus = UNSET
        else:
            focus = self.focus

        force: bool | None | Unset
        if isinstance(self.force, Unset):
            force = UNSET
        else:
            force = self.force

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review_id": review_id,
            }
        )
        if count is not UNSET:
            field_dict["count"] = count
        if intelligence_class is not UNSET:
            field_dict["intelligence_class"] = intelligence_class
        if revision is not UNSET:
            field_dict["revision"] = revision
        if with_comments is not UNSET:
            field_dict["with_comments"] = with_comments
        if focus is not UNSET:
            field_dict["focus"] = focus
        if force is not UNSET:
            field_dict["force"] = force

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        review_id = d.pop("review_id")

        def _parse_count(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        count = _parse_count(d.pop("count", UNSET))

        def _parse_intelligence_class(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        intelligence_class = _parse_intelligence_class(d.pop("intelligence_class", UNSET))

        def _parse_revision(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        revision = _parse_revision(d.pop("revision", UNSET))

        def _parse_with_comments(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        with_comments = _parse_with_comments(d.pop("with_comments", UNSET))

        def _parse_focus(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        focus = _parse_focus(d.pop("focus", UNSET))

        def _parse_force(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        force = _parse_force(d.pop("force", UNSET))

        review_dispatch_request = cls(
            review_id=review_id,
            count=count,
            intelligence_class=intelligence_class,
            revision=revision,
            with_comments=with_comments,
            focus=focus,
            force=force,
        )

        review_dispatch_request.additional_properties = d
        return review_dispatch_request

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
