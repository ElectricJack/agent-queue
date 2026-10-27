from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

T = TypeVar("T", bound="TaskClaimAbortRequest")


@_attrs_define
class TaskClaimAbortRequest:
    """
    Attributes:
        task_id (str): Preparing task ID.
        session_id (str): Exact preparing session ID.
        claim_epoch (int):
        instance_token (str):
        reason (str):
    """

    task_id: str
    session_id: str
    claim_epoch: int
    instance_token: str
    reason: str
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        session_id = self.session_id

        claim_epoch = self.claim_epoch

        instance_token = self.instance_token

        reason = self.reason

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "session_id": session_id,
                "claim_epoch": claim_epoch,
                "instance_token": instance_token,
                "reason": reason,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        session_id = d.pop("session_id")

        claim_epoch = d.pop("claim_epoch")

        instance_token = d.pop("instance_token")

        reason = d.pop("reason")

        task_claim_abort_request = cls(
            task_id=task_id,
            session_id=session_id,
            claim_epoch=claim_epoch,
            instance_token=instance_token,
            reason=reason,
        )

        task_claim_abort_request.additional_properties = d
        return task_claim_abort_request

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
