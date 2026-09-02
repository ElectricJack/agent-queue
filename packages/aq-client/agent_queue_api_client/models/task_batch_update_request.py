from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.task_batch_update_request_payload import TaskBatchUpdateRequestPayload


T = TypeVar("T", bound="TaskBatchUpdateRequest")


@_attrs_define
class TaskBatchUpdateRequest:
    """
    Attributes:
        proposal_id (str): Proposal to replace.
        payload (TaskBatchUpdateRequestPayload): The replacement graph: ``{"tasks": [...], "edges": [...]}`` in the same
            shape task_batch_propose takes.
    """

    proposal_id: str
    payload: TaskBatchUpdateRequestPayload
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        proposal_id = self.proposal_id

        payload = self.payload.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "proposal_id": proposal_id,
                "payload": payload,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_batch_update_request_payload import TaskBatchUpdateRequestPayload

        d = dict(src_dict)
        proposal_id = d.pop("proposal_id")

        payload = TaskBatchUpdateRequestPayload.from_dict(d.pop("payload"))

        task_batch_update_request = cls(
            proposal_id=proposal_id,
            payload=payload,
        )

        task_batch_update_request.additional_properties = d
        return task_batch_update_request

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
