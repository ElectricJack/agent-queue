from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_batch_validate_response_advisory_item import TaskBatchValidateResponseAdvisoryItem
    from ..models.task_batch_validate_response_gaps_item import TaskBatchValidateResponseGapsItem
    from ..models.task_batch_validate_response_hard_item import TaskBatchValidateResponseHardItem


T = TypeVar("T", bound="TaskBatchValidateResponse")


@_attrs_define
class TaskBatchValidateResponse:
    """``task_batch_validate`` — the automatic-check report; nothing is written.

    Attributes:
        success (bool | Unset):  Default: True.
        project_id (None | str | Unset):
        batch_id (None | str | Unset):
        passed (bool | Unset):  Default: False.
        retryable (bool | Unset):  Default: False.
        hard (list[TaskBatchValidateResponseHardItem] | Unset):
        advisory (list[TaskBatchValidateResponseAdvisoryItem] | Unset):
        gaps (list[TaskBatchValidateResponseGapsItem] | Unset):
    """

    success: bool | Unset = True
    project_id: None | str | Unset = UNSET
    batch_id: None | str | Unset = UNSET
    passed: bool | Unset = False
    retryable: bool | Unset = False
    hard: list[TaskBatchValidateResponseHardItem] | Unset = UNSET
    advisory: list[TaskBatchValidateResponseAdvisoryItem] | Unset = UNSET
    gaps: list[TaskBatchValidateResponseGapsItem] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        batch_id: None | str | Unset
        if isinstance(self.batch_id, Unset):
            batch_id = UNSET
        else:
            batch_id = self.batch_id

        passed = self.passed

        retryable = self.retryable

        hard: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.hard, Unset):
            hard = []
            for hard_item_data in self.hard:
                hard_item = hard_item_data.to_dict()
                hard.append(hard_item)

        advisory: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.advisory, Unset):
            advisory = []
            for advisory_item_data in self.advisory:
                advisory_item = advisory_item_data.to_dict()
                advisory.append(advisory_item)

        gaps: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.gaps, Unset):
            gaps = []
            for gaps_item_data in self.gaps:
                gaps_item = gaps_item_data.to_dict()
                gaps.append(gaps_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if batch_id is not UNSET:
            field_dict["batch_id"] = batch_id
        if passed is not UNSET:
            field_dict["passed"] = passed
        if retryable is not UNSET:
            field_dict["retryable"] = retryable
        if hard is not UNSET:
            field_dict["hard"] = hard
        if advisory is not UNSET:
            field_dict["advisory"] = advisory
        if gaps is not UNSET:
            field_dict["gaps"] = gaps

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_batch_validate_response_advisory_item import TaskBatchValidateResponseAdvisoryItem
        from ..models.task_batch_validate_response_gaps_item import TaskBatchValidateResponseGapsItem
        from ..models.task_batch_validate_response_hard_item import TaskBatchValidateResponseHardItem

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_batch_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        batch_id = _parse_batch_id(d.pop("batch_id", UNSET))

        passed = d.pop("passed", UNSET)

        retryable = d.pop("retryable", UNSET)

        _hard = d.pop("hard", UNSET)
        hard: list[TaskBatchValidateResponseHardItem] | Unset = UNSET
        if _hard is not UNSET:
            hard = []
            for hard_item_data in _hard:
                hard_item = TaskBatchValidateResponseHardItem.from_dict(hard_item_data)

                hard.append(hard_item)

        _advisory = d.pop("advisory", UNSET)
        advisory: list[TaskBatchValidateResponseAdvisoryItem] | Unset = UNSET
        if _advisory is not UNSET:
            advisory = []
            for advisory_item_data in _advisory:
                advisory_item = TaskBatchValidateResponseAdvisoryItem.from_dict(advisory_item_data)

                advisory.append(advisory_item)

        _gaps = d.pop("gaps", UNSET)
        gaps: list[TaskBatchValidateResponseGapsItem] | Unset = UNSET
        if _gaps is not UNSET:
            gaps = []
            for gaps_item_data in _gaps:
                gaps_item = TaskBatchValidateResponseGapsItem.from_dict(gaps_item_data)

                gaps.append(gaps_item)

        task_batch_validate_response = cls(
            success=success,
            project_id=project_id,
            batch_id=batch_id,
            passed=passed,
            retryable=retryable,
            hard=hard,
            advisory=advisory,
            gaps=gaps,
        )

        task_batch_validate_response.additional_properties = d
        return task_batch_validate_response

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
