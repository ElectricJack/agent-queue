from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskShowRequest")


@_attrs_define
class TaskShowRequest:
    """
    Attributes:
        task_id (str): Task id to show.
        field (None | str | Unset): Read one complete field in bounded pages; structured fields are serialized JSON.
        offset (int | Unset): Character offset for a field page. Default: 0.
        limit (int | Unset): Maximum characters in a field page. Default: 4096.
    """

    task_id: str
    field: None | str | Unset = UNSET
    offset: int | Unset = 0
    limit: int | Unset = 4096
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        field: None | str | Unset
        if isinstance(self.field, Unset):
            field = UNSET
        else:
            field = self.field

        offset = self.offset

        limit = self.limit

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if field is not UNSET:
            field_dict["field"] = field
        if offset is not UNSET:
            field_dict["offset"] = offset
        if limit is not UNSET:
            field_dict["limit"] = limit

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        def _parse_field(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        field = _parse_field(d.pop("field", UNSET))

        offset = d.pop("offset", UNSET)

        limit = d.pop("limit", UNSET)

        task_show_request = cls(
            task_id=task_id,
            field=field,
            offset=offset,
            limit=limit,
        )

        task_show_request.additional_properties = d
        return task_show_request

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
