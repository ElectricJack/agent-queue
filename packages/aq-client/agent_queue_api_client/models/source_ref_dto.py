from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="SourceRefDTO")


@_attrs_define
class SourceRefDTO:
    """Where in the authoring Markdown this element came from.

    Attributes:
        path (str):
        start_line (int):
        end_line (int):
        heading (None | str | Unset):
        excerpt (None | str | Unset):
    """

    path: str
    start_line: int
    end_line: int
    heading: None | str | Unset = UNSET
    excerpt: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        path = self.path

        start_line = self.start_line

        end_line = self.end_line

        heading: None | str | Unset
        if isinstance(self.heading, Unset):
            heading = UNSET
        else:
            heading = self.heading

        excerpt: None | str | Unset
        if isinstance(self.excerpt, Unset):
            excerpt = UNSET
        else:
            excerpt = self.excerpt

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
            }
        )
        if heading is not UNSET:
            field_dict["heading"] = heading
        if excerpt is not UNSET:
            field_dict["excerpt"] = excerpt

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        path = d.pop("path")

        start_line = d.pop("start_line")

        end_line = d.pop("end_line")

        def _parse_heading(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        heading = _parse_heading(d.pop("heading", UNSET))

        def _parse_excerpt(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        excerpt = _parse_excerpt(d.pop("excerpt", UNSET))

        source_ref_dto = cls(
            path=path,
            start_line=start_line,
            end_line=end_line,
            heading=heading,
            excerpt=excerpt,
        )

        return source_ref_dto
