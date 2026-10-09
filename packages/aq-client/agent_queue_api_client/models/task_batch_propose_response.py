from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_batch_propose_response_diff_type_0 import TaskBatchProposeResponseDiffType0


T = TypeVar("T", bound="TaskBatchProposeResponse")


@_attrs_define
class TaskBatchProposeResponse:
    """A proposal is created, not applied — unless ingestion applies it live.

    A batch carrying a live spec-ingest role's approved-document authority is
    applied in the same call: ``committed`` is then true and ``task_ids`` is
    that batch's receipt, exactly as a commit's would be. Every other proposal
    stages in ``ready`` and waits for approval.

        Attributes:
            success (bool | Unset):  Default: True.
            proposal_id (None | str | Unset):
            dry_run (bool | Unset):  Default: False.
            committed (bool | Unset):  Default: False.
            task_ids (list[str] | None | Unset):
            diff (None | TaskBatchProposeResponseDiffType0 | Unset):
    """

    success: bool | Unset = True
    proposal_id: None | str | Unset = UNSET
    dry_run: bool | Unset = False
    committed: bool | Unset = False
    task_ids: list[str] | None | Unset = UNSET
    diff: None | TaskBatchProposeResponseDiffType0 | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.task_batch_propose_response_diff_type_0 import TaskBatchProposeResponseDiffType0

        success = self.success

        proposal_id: None | str | Unset
        if isinstance(self.proposal_id, Unset):
            proposal_id = UNSET
        else:
            proposal_id = self.proposal_id

        dry_run = self.dry_run

        committed = self.committed

        task_ids: list[str] | None | Unset
        if isinstance(self.task_ids, Unset):
            task_ids = UNSET
        elif isinstance(self.task_ids, list):
            task_ids = self.task_ids

        else:
            task_ids = self.task_ids

        diff: dict[str, Any] | None | Unset
        if isinstance(self.diff, Unset):
            diff = UNSET
        elif isinstance(self.diff, TaskBatchProposeResponseDiffType0):
            diff = self.diff.to_dict()
        else:
            diff = self.diff

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if proposal_id is not UNSET:
            field_dict["proposal_id"] = proposal_id
        if dry_run is not UNSET:
            field_dict["dry_run"] = dry_run
        if committed is not UNSET:
            field_dict["committed"] = committed
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids
        if diff is not UNSET:
            field_dict["diff"] = diff

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_batch_propose_response_diff_type_0 import TaskBatchProposeResponseDiffType0

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        def _parse_proposal_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        proposal_id = _parse_proposal_id(d.pop("proposal_id", UNSET))

        dry_run = d.pop("dry_run", UNSET)

        committed = d.pop("committed", UNSET)

        def _parse_task_ids(data: object) -> list[str] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                task_ids_type_0 = cast(list[str], data)

                return task_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[str] | None | Unset, data)

        task_ids = _parse_task_ids(d.pop("task_ids", UNSET))

        def _parse_diff(data: object) -> None | TaskBatchProposeResponseDiffType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                diff_type_0 = TaskBatchProposeResponseDiffType0.from_dict(data)

                return diff_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskBatchProposeResponseDiffType0 | Unset, data)

        diff = _parse_diff(d.pop("diff", UNSET))

        task_batch_propose_response = cls(
            success=success,
            proposal_id=proposal_id,
            dry_run=dry_run,
            committed=committed,
            task_ids=task_ids,
            diff=diff,
        )

        task_batch_propose_response.additional_properties = d
        return task_batch_propose_response

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
