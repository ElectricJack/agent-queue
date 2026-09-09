from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskActivityModelTotal")


@_attrs_define
class TaskActivityModelTotal:
    """
    Attributes:
        model (None | str | Unset):
        tasks (int | Unset):  Default: 0.
        attempts (int | Unset):  Default: 0.
    """

    model: None | str | Unset = UNSET
    tasks: int | Unset = 0
    attempts: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        model: None | str | Unset
        if isinstance(self.model, Unset):
            model = UNSET
        else:
            model = self.model

        tasks = self.tasks

        attempts = self.attempts

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if model is not UNSET:
            field_dict["model"] = model
        if tasks is not UNSET:
            field_dict["tasks"] = tasks
        if attempts is not UNSET:
            field_dict["attempts"] = attempts

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_model(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model = _parse_model(d.pop("model", UNSET))

        tasks = d.pop("tasks", UNSET)

        attempts = d.pop("attempts", UNSET)

        task_activity_model_total = cls(
            model=model,
            tasks=tasks,
            attempts=attempts,
        )

        task_activity_model_total.additional_properties = d
        return task_activity_model_total

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
