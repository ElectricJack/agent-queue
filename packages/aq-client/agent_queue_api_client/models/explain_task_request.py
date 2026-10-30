from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ExplainTaskRequest")


@_attrs_define
class ExplainTaskRequest:
    """
    Attributes:
        task_id (None | str | Unset): Task id to explain
        task_ids (list[Any] | None | Unset): Explain these tasks in one request instead (at most 100); returns one entry
            per task under explanations
    """

    task_id: None | str | Unset = UNSET
    task_ids: list[Any] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        task_ids: list[Any] | None | Unset
        if isinstance(self.task_ids, Unset):
            task_ids = UNSET
        elif isinstance(self.task_ids, list):
            task_ids = self.task_ids

        else:
            task_ids = self.task_ids

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_task_ids(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                task_ids_type_0 = cast(list[Any], data)

                return task_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        task_ids = _parse_task_ids(d.pop("task_ids", UNSET))

        explain_task_request = cls(
            task_id=task_id,
            task_ids=task_ids,
        )

        explain_task_request.additional_properties = d
        return explain_task_request

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
