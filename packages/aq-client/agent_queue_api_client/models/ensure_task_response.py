from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="EnsureTaskResponse")


@_attrs_define
class EnsureTaskResponse:
    """
    Attributes:
        created (bool):
        success (bool | Unset):  Default: True.
        task_id (None | str | Unset):
        restarted (bool | Unset):  Default: False.
        skipped (bool | Unset):  Default: False.
        reason (None | str | Unset):
    """

    created: bool
    success: bool | Unset = True
    task_id: None | str | Unset = UNSET
    restarted: bool | Unset = False
    skipped: bool | Unset = False
    reason: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        created = self.created

        success = self.success

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        restarted = self.restarted

        skipped = self.skipped

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "created": created,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if restarted is not UNSET:
            field_dict["restarted"] = restarted
        if skipped is not UNSET:
            field_dict["skipped"] = skipped
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        created = d.pop("created")

        success = d.pop("success", UNSET)

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        restarted = d.pop("restarted", UNSET)

        skipped = d.pop("skipped", UNSET)

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        ensure_task_response = cls(
            created=created,
            success=success,
            task_id=task_id,
            restarted=restarted,
            skipped=skipped,
            reason=reason,
        )

        ensure_task_response.additional_properties = d
        return ensure_task_response

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
