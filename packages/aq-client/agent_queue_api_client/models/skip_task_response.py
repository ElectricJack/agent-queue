from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_ref import TaskRef


T = TypeVar("T", bound="SkipTaskResponse")


@_attrs_define
class SkipTaskResponse:
    """
    Attributes:
        skipped (str):
        unblocked_count (int | Unset):  Default: 0.
        unblocked (list[TaskRef] | Unset):
    """

    skipped: str
    unblocked_count: int | Unset = 0
    unblocked: list[TaskRef] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        skipped = self.skipped

        unblocked_count = self.unblocked_count

        unblocked: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.unblocked, Unset):
            unblocked = []
            for unblocked_item_data in self.unblocked:
                unblocked_item = unblocked_item_data.to_dict()
                unblocked.append(unblocked_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "skipped": skipped,
            }
        )
        if unblocked_count is not UNSET:
            field_dict["unblocked_count"] = unblocked_count
        if unblocked is not UNSET:
            field_dict["unblocked"] = unblocked

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_ref import TaskRef

        d = dict(src_dict)
        skipped = d.pop("skipped")

        unblocked_count = d.pop("unblocked_count", UNSET)

        _unblocked = d.pop("unblocked", UNSET)
        unblocked: list[TaskRef] | Unset = UNSET
        if _unblocked is not UNSET:
            unblocked = []
            for unblocked_item_data in _unblocked:
                unblocked_item = TaskRef.from_dict(unblocked_item_data)

                unblocked.append(unblocked_item)

        skip_task_response = cls(
            skipped=skipped,
            unblocked_count=unblocked_count,
            unblocked=unblocked,
        )

        skip_task_response.additional_properties = d
        return skip_task_response

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
