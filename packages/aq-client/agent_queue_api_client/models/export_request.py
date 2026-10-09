from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

T = TypeVar("T", bound="ExportRequest")


@_attrs_define
class ExportRequest:
    """
    Attributes:
        project_id (str):
        name (None | str | Unset):
        path (None | str | Unset):
        expected_checksum (None | str | Unset):
    """

    project_id: str
    name: None | str | Unset = UNSET
    path: None | str | Unset = UNSET
    expected_checksum: None | str | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        name: None | str | Unset
        if isinstance(self.name, Unset):
            name = UNSET
        else:
            name = self.name

        path: None | str | Unset
        if isinstance(self.path, Unset):
            path = UNSET
        else:
            path = self.path

        expected_checksum: None | str | Unset
        if isinstance(self.expected_checksum, Unset):
            expected_checksum = UNSET
        else:
            expected_checksum = self.expected_checksum

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name
        if path is not UNSET:
            field_dict["path"] = path
        if expected_checksum is not UNSET:
            field_dict["expected_checksum"] = expected_checksum

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_name(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        name = _parse_name(d.pop("name", UNSET))

        def _parse_path(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        path = _parse_path(d.pop("path", UNSET))

        def _parse_expected_checksum(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_checksum = _parse_expected_checksum(d.pop("expected_checksum", UNSET))

        export_request = cls(
            project_id=project_id,
            name=name,
            path=path,
            expected_checksum=expected_checksum,
        )

        return export_request
