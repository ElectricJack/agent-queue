from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_batch_ack_response_diff_type_0 import TaskBatchAckResponseDiffType0


T = TypeVar("T", bound="TaskBatchAckResponse")


@_attrs_define
class TaskBatchAckResponse:
    """``task_batch_update`` / ``task_batch_discard`` — bare acknowledgement.

    Attributes:
        success (bool | Unset):  Default: True.
        diff (None | TaskBatchAckResponseDiffType0 | Unset):
    """

    success: bool | Unset = True
    diff: None | TaskBatchAckResponseDiffType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.task_batch_ack_response_diff_type_0 import TaskBatchAckResponseDiffType0

        success = self.success

        diff: dict[str, Any] | None | Unset
        if isinstance(self.diff, Unset):
            diff = UNSET
        elif isinstance(self.diff, TaskBatchAckResponseDiffType0):
            diff = self.diff.to_dict()
        else:
            diff = self.diff

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if diff is not UNSET:
            field_dict["diff"] = diff

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_batch_ack_response_diff_type_0 import TaskBatchAckResponseDiffType0

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_diff(data: object) -> None | TaskBatchAckResponseDiffType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                diff_type_0 = TaskBatchAckResponseDiffType0.from_dict(data)

                return diff_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskBatchAckResponseDiffType0 | Unset, data)

        diff = _parse_diff(d.pop("diff", UNSET))

        task_batch_ack_response = cls(
            success=success,
            diff=diff,
        )

        task_batch_ack_response.additional_properties = d
        return task_batch_ack_response

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
