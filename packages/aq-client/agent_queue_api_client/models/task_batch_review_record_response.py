from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.task_batch_review_record_response_state import TaskBatchReviewRecordResponseState
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_batch_review_record_response_receipt import TaskBatchReviewRecordResponseReceipt


T = TypeVar("T", bound="TaskBatchReviewRecordResponse")


@_attrs_define
class TaskBatchReviewRecordResponse:
    """The durable fidelity receipt, including a scope ask when one was raised.

    Attributes:
        batch_id (str):
        state (TaskBatchReviewRecordResponseState):
        passing (bool):
        receipt (TaskBatchReviewRecordResponseReceipt):
        replayed (bool):
        success (bool | Unset):  Default: True.
        ask_id (None | str | Unset):
    """

    batch_id: str
    state: TaskBatchReviewRecordResponseState
    passing: bool
    receipt: TaskBatchReviewRecordResponseReceipt
    replayed: bool
    success: bool | Unset = True
    ask_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        batch_id = self.batch_id

        state = self.state.value

        passing = self.passing

        receipt = self.receipt.to_dict()

        replayed = self.replayed

        success = self.success

        ask_id: None | str | Unset
        if isinstance(self.ask_id, Unset):
            ask_id = UNSET
        else:
            ask_id = self.ask_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "batch_id": batch_id,
                "state": state,
                "passing": passing,
                "receipt": receipt,
                "replayed": replayed,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if ask_id is not UNSET:
            field_dict["ask_id"] = ask_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_batch_review_record_response_receipt import TaskBatchReviewRecordResponseReceipt

        d = dict(src_dict)
        batch_id = d.pop("batch_id")

        state = TaskBatchReviewRecordResponseState(d.pop("state"))

        passing = d.pop("passing")

        receipt = TaskBatchReviewRecordResponseReceipt.from_dict(d.pop("receipt"))

        replayed = d.pop("replayed")

        success = d.pop("success", UNSET)

        def _parse_ask_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        ask_id = _parse_ask_id(d.pop("ask_id", UNSET))

        task_batch_review_record_response = cls(
            batch_id=batch_id,
            state=state,
            passing=passing,
            receipt=receipt,
            replayed=replayed,
            success=success,
            ask_id=ask_id,
        )

        task_batch_review_record_response.additional_properties = d
        return task_batch_review_record_response

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
