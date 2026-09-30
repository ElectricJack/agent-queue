from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="ReviewAttachmentAddRequest")


@_attrs_define
class ReviewAttachmentAddRequest:
    """
    Attributes:
        review_id (str):
        revision (int):
        data_base64 (str):
        content_type (str):
        caption (str):
        view_id (str):
        candidate_id (str):
    """

    review_id: str
    revision: int
    data_base64: str
    content_type: str
    caption: str
    view_id: str
    candidate_id: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        review_id = self.review_id

        revision = self.revision

        data_base64 = self.data_base64

        content_type = self.content_type

        caption = self.caption

        view_id = self.view_id

        candidate_id = self.candidate_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review_id": review_id,
                "revision": revision,
                "data_base64": data_base64,
                "content_type": content_type,
                "caption": caption,
                "view_id": view_id,
                "candidate_id": candidate_id,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        review_id = d.pop("review_id")

        revision = d.pop("revision")

        data_base64 = d.pop("data_base64")

        content_type = d.pop("content_type")

        caption = d.pop("caption")

        view_id = d.pop("view_id")

        candidate_id = d.pop("candidate_id")

        review_attachment_add_request = cls(
            review_id=review_id,
            revision=revision,
            data_base64=data_base64,
            content_type=content_type,
            caption=caption,
            view_id=view_id,
            candidate_id=candidate_id,
        )

        review_attachment_add_request.additional_properties = d
        return review_attachment_add_request

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
