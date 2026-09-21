from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.review_record import ReviewRecord
    from ..models.review_show_response_comments_type_0_item import ReviewShowResponseCommentsType0Item
    from ..models.review_show_response_diff_type_0_item import ReviewShowResponseDiffType0Item
    from ..models.review_show_response_revision import ReviewShowResponseRevision
    from ..models.review_show_response_revisions_item import ReviewShowResponseRevisionsItem


T = TypeVar("T", bound="ReviewShowResponse")


@_attrs_define
class ReviewShowResponse:
    """
    Attributes:
        review (ReviewRecord):
        revision (ReviewShowResponseRevision):
        vault_state (str):
        success (bool | Unset):  Default: True.
        revisions (list[ReviewShowResponseRevisionsItem] | Unset):
        comments (list[ReviewShowResponseCommentsType0Item] | None | Unset):
        diff (list[ReviewShowResponseDiffType0Item] | None | Unset):
    """

    review: ReviewRecord
    revision: ReviewShowResponseRevision
    vault_state: str
    success: bool | Unset = True
    revisions: list[ReviewShowResponseRevisionsItem] | Unset = UNSET
    comments: list[ReviewShowResponseCommentsType0Item] | None | Unset = UNSET
    diff: list[ReviewShowResponseDiffType0Item] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        review = self.review.to_dict()

        revision = self.revision.to_dict()

        vault_state = self.vault_state

        success = self.success

        revisions: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.revisions, Unset):
            revisions = []
            for revisions_item_data in self.revisions:
                revisions_item = revisions_item_data.to_dict()
                revisions.append(revisions_item)

        comments: list[dict[str, Any]] | None | Unset
        if isinstance(self.comments, Unset):
            comments = UNSET
        elif isinstance(self.comments, list):
            comments = []
            for comments_type_0_item_data in self.comments:
                comments_type_0_item = comments_type_0_item_data.to_dict()
                comments.append(comments_type_0_item)

        else:
            comments = self.comments

        diff: list[dict[str, Any]] | None | Unset
        if isinstance(self.diff, Unset):
            diff = UNSET
        elif isinstance(self.diff, list):
            diff = []
            for diff_type_0_item_data in self.diff:
                diff_type_0_item = diff_type_0_item_data.to_dict()
                diff.append(diff_type_0_item)

        else:
            diff = self.diff

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review": review,
                "revision": revision,
                "vault_state": vault_state,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if revisions is not UNSET:
            field_dict["revisions"] = revisions
        if comments is not UNSET:
            field_dict["comments"] = comments
        if diff is not UNSET:
            field_dict["diff"] = diff

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.review_record import ReviewRecord
        from ..models.review_show_response_comments_type_0_item import ReviewShowResponseCommentsType0Item
        from ..models.review_show_response_diff_type_0_item import ReviewShowResponseDiffType0Item
        from ..models.review_show_response_revision import ReviewShowResponseRevision
        from ..models.review_show_response_revisions_item import ReviewShowResponseRevisionsItem

        d = dict(src_dict)
        review = ReviewRecord.from_dict(d.pop("review"))

        revision = ReviewShowResponseRevision.from_dict(d.pop("revision"))

        vault_state = d.pop("vault_state")

        success = d.pop("success", UNSET)

        _revisions = d.pop("revisions", UNSET)
        revisions: list[ReviewShowResponseRevisionsItem] | Unset = UNSET
        if _revisions is not UNSET:
            revisions = []
            for revisions_item_data in _revisions:
                revisions_item = ReviewShowResponseRevisionsItem.from_dict(revisions_item_data)

                revisions.append(revisions_item)

        def _parse_comments(data: object) -> list[ReviewShowResponseCommentsType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                comments_type_0 = []
                _comments_type_0 = data
                for comments_type_0_item_data in _comments_type_0:
                    comments_type_0_item = ReviewShowResponseCommentsType0Item.from_dict(comments_type_0_item_data)

                    comments_type_0.append(comments_type_0_item)

                return comments_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[ReviewShowResponseCommentsType0Item] | None | Unset, data)

        comments = _parse_comments(d.pop("comments", UNSET))

        def _parse_diff(data: object) -> list[ReviewShowResponseDiffType0Item] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                diff_type_0 = []
                _diff_type_0 = data
                for diff_type_0_item_data in _diff_type_0:
                    diff_type_0_item = ReviewShowResponseDiffType0Item.from_dict(diff_type_0_item_data)

                    diff_type_0.append(diff_type_0_item)

                return diff_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[ReviewShowResponseDiffType0Item] | None | Unset, data)

        diff = _parse_diff(d.pop("diff", UNSET))

        review_show_response = cls(
            review=review,
            revision=revision,
            vault_state=vault_state,
            success=success,
            revisions=revisions,
            comments=comments,
            diff=diff,
        )

        review_show_response.additional_properties = d
        return review_show_response

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
