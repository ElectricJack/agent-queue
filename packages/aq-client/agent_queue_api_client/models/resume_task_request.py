from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="ResumeTaskRequest")


@_attrs_define
class ResumeTaskRequest:
    """
    Attributes:
        task_id (str): Task ID to resume
        recover_stale_pause (bool | None | Unset): Operator/supervisor recovery of a stale pause on an unchanged
            retained source; still requires writer stop proof and ordinary checkpoint cleanup.
    """

    task_id: str
    recover_stale_pause: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        recover_stale_pause: bool | None | Unset
        if isinstance(self.recover_stale_pause, Unset):
            recover_stale_pause = UNSET
        else:
            recover_stale_pause = self.recover_stale_pause

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if recover_stale_pause is not UNSET:
            field_dict["recover_stale_pause"] = recover_stale_pause

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        def _parse_recover_stale_pause(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        recover_stale_pause = _parse_recover_stale_pause(d.pop("recover_stale_pause", UNSET))

        resume_task_request = cls(
            task_id=task_id,
            recover_stale_pause=recover_stale_pause,
        )

        resume_task_request.additional_properties = d
        return resume_task_request

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
