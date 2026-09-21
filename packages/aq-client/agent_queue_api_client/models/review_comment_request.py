from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ReviewCommentRequest")


@_attrs_define
class ReviewCommentRequest:
    """
    Attributes:
        review_id (str):
        revision (int):
        body (str):
        quote (None | str | Unset):
        heading_path (list[Any] | None | Unset):
    """

    review_id: str
    revision: int
    body: str
    quote: None | str | Unset = UNSET
    heading_path: list[Any] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        review_id = self.review_id

        revision = self.revision

        body = self.body

        quote: None | str | Unset
        if isinstance(self.quote, Unset):
            quote = UNSET
        else:
            quote = self.quote

        heading_path: list[Any] | None | Unset
        if isinstance(self.heading_path, Unset):
            heading_path = UNSET
        elif isinstance(self.heading_path, list):
            heading_path = self.heading_path

        else:
            heading_path = self.heading_path

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "review_id": review_id,
                "revision": revision,
                "body": body,
            }
        )
        if quote is not UNSET:
            field_dict["quote"] = quote
        if heading_path is not UNSET:
            field_dict["heading_path"] = heading_path

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        review_id = d.pop("review_id")

        revision = d.pop("revision")

        body = d.pop("body")

        def _parse_quote(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        quote = _parse_quote(d.pop("quote", UNSET))

        def _parse_heading_path(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                heading_path_type_0 = cast(list[Any], data)

                return heading_path_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        heading_path = _parse_heading_path(d.pop("heading_path", UNSET))

        review_comment_request = cls(
            review_id=review_id,
            revision=revision,
            body=body,
            quote=quote,
            heading_path=heading_path,
        )

        review_comment_request.additional_properties = d
        return review_comment_request

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
