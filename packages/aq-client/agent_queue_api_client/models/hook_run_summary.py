from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="HookRunSummary")


@_attrs_define
class HookRunSummary:
    """
    Attributes:
        id (str):
        status (str | Unset):  Default: ''.
        trigger_reason (str | Unset):  Default: ''.
        tokens_used (int | Unset):  Default: 0.
        skipped_reason (None | str | Unset):
        started_at (float | None | Unset):
        completed_at (float | None | Unset):
    """

    id: str
    status: str | Unset = ""
    trigger_reason: str | Unset = ""
    tokens_used: int | Unset = 0
    skipped_reason: None | str | Unset = UNSET
    started_at: float | None | Unset = UNSET
    completed_at: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        status = self.status

        trigger_reason = self.trigger_reason

        tokens_used = self.tokens_used

        skipped_reason: None | str | Unset
        if isinstance(self.skipped_reason, Unset):
            skipped_reason = UNSET
        else:
            skipped_reason = self.skipped_reason

        started_at: float | None | Unset
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        else:
            started_at = self.started_at

        completed_at: float | None | Unset
        if isinstance(self.completed_at, Unset):
            completed_at = UNSET
        else:
            completed_at = self.completed_at

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
            }
        )
        if status is not UNSET:
            field_dict["status"] = status
        if trigger_reason is not UNSET:
            field_dict["trigger_reason"] = trigger_reason
        if tokens_used is not UNSET:
            field_dict["tokens_used"] = tokens_used
        if skipped_reason is not UNSET:
            field_dict["skipped_reason"] = skipped_reason
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        id = d.pop("id")

        status = d.pop("status", UNSET)

        trigger_reason = d.pop("trigger_reason", UNSET)

        tokens_used = d.pop("tokens_used", UNSET)

        def _parse_skipped_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        skipped_reason = _parse_skipped_reason(d.pop("skipped_reason", UNSET))

        def _parse_started_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))

        def _parse_completed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completed_at = _parse_completed_at(d.pop("completed_at", UNSET))

        hook_run_summary = cls(
            id=id,
            status=status,
            trigger_reason=trigger_reason,
            tokens_used=tokens_used,
            skipped_reason=skipped_reason,
            started_at=started_at,
            completed_at=completed_at,
        )

        hook_run_summary.additional_properties = d
        return hook_run_summary

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
