from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="TaskDeliverRequest")


@_attrs_define
class TaskDeliverRequest:
    """
    Attributes:
        task_id (str): The BLOCKED task whose recorded branch is delivered.
        reason (str): Audit reason; recorded on the task and in the merge commit.
        dry_run (bool | Unset): Report the delivery plan without pushing or completing. Default: False.
        expected_head (None | str | Unset): The branch head you inspected (head_sha from a dry run, full or 7+
            characters); a branch that moved since is refused.
    """

    task_id: str
    reason: str
    dry_run: bool | Unset = False
    expected_head: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        reason = self.reason

        dry_run = self.dry_run

        expected_head: None | str | Unset
        if isinstance(self.expected_head, Unset):
            expected_head = UNSET
        else:
            expected_head = self.expected_head

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "reason": reason,
            }
        )
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if expected_head is not UNSET:
            field_dict["expected_head"] = expected_head

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        reason = d.pop("reason")

        dry_run = d.pop("dry_run", UNSET)

        def _parse_expected_head(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_head = _parse_expected_head(d.pop("expected_head", UNSET))

        task_deliver_request = cls(
            task_id=task_id,
            reason=reason,
            dry_run=dry_run,
            expected_head=expected_head,
        )

        task_deliver_request.additional_properties = d
        return task_deliver_request

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
