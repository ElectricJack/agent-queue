from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TestSelectionListRequest")


@_attrs_define
class TestSelectionListRequest:
    """
    Attributes:
        project_id (str):
        task_id (None | str | Unset):
        limit (int | Unset):  Default: 50.
        before (float | None | Unset):
    """

    project_id: str
    task_id: None | str | Unset = UNSET
    limit: int | Unset = 50
    before: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        limit = self.limit

        before: float | None | Unset
        if isinstance(self.before, Unset):
            before = UNSET
        else:
            before = self.before

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if limit is not UNSET:
            field_dict["limit"] = limit
        if before is not UNSET:
            field_dict["before"] = before

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        limit = d.pop("limit", UNSET)

        def _parse_before(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        before = _parse_before(d.pop("before", UNSET))

        test_selection_list_request = cls(
            project_id=project_id,
            task_id=task_id,
            limit=limit,
            before=before,
        )

        test_selection_list_request.additional_properties = d
        return test_selection_list_request

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
