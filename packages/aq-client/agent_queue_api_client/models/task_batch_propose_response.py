from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..models.task_batch_propose_response_status_type_0 import TaskBatchProposeResponseStatusType0
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_batch_propose_response_diff_type_0 import TaskBatchProposeResponseDiffType0
    from ..models.task_batch_propose_response_lineage_type_0 import TaskBatchProposeResponseLineageType0
    from ..models.task_batch_propose_response_members_type_0 import TaskBatchProposeResponseMembersType0
    from ..models.task_batch_propose_response_release_receipt_type_0 import TaskBatchProposeResponseReleaseReceiptType0


T = TypeVar("T", bound="TaskBatchProposeResponse")


@_attrs_define
class TaskBatchProposeResponse:
    """Ordinary proposals stage for approval; ingestion materializes held work.

    Design-kind ingestion returns a released batch with a server receipt.
    Implementation ingestion returns held members pending fidelity review.

        Attributes:
            success (bool | Unset):  Default: True.
            proposal_id (None | str | Unset):
            dry_run (bool | Unset):  Default: False.
            committed (bool | Unset):  Default: False.
            already_committed (bool | Unset):  Default: False.
            task_ids (list[str] | None | Unset):
            diff (None | TaskBatchProposeResponseDiffType0 | Unset):
            status (None | TaskBatchProposeResponseStatusType0 | Unset):
            batch_id (None | str | Unset):
            state (None | str | Unset):
            structure_revision (int | None | Unset):
            structure_digest (None | str | Unset):
            members (None | TaskBatchProposeResponseMembersType0 | Unset):
            lineage (None | TaskBatchProposeResponseLineageType0 | Unset):
            release_receipt (None | TaskBatchProposeResponseReleaseReceiptType0 | Unset):
            reviewer_task_id (None | str | Unset):
            replayed (bool | None | Unset):
    """

    success: bool | Unset = True
    proposal_id: None | str | Unset = UNSET
    dry_run: bool | Unset = False
    committed: bool | Unset = False
    already_committed: bool | Unset = False
    task_ids: list[str] | None | Unset = UNSET
    diff: None | TaskBatchProposeResponseDiffType0 | Unset = UNSET
    status: None | TaskBatchProposeResponseStatusType0 | Unset = UNSET
    batch_id: None | str | Unset = UNSET
    state: None | str | Unset = UNSET
    structure_revision: int | None | Unset = UNSET
    structure_digest: None | str | Unset = UNSET
    members: None | TaskBatchProposeResponseMembersType0 | Unset = UNSET
    lineage: None | TaskBatchProposeResponseLineageType0 | Unset = UNSET
    release_receipt: None | TaskBatchProposeResponseReleaseReceiptType0 | Unset = UNSET
    reviewer_task_id: None | str | Unset = UNSET
    replayed: bool | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.task_batch_propose_response_diff_type_0 import TaskBatchProposeResponseDiffType0
        from ..models.task_batch_propose_response_lineage_type_0 import TaskBatchProposeResponseLineageType0
        from ..models.task_batch_propose_response_members_type_0 import TaskBatchProposeResponseMembersType0
        from ..models.task_batch_propose_response_release_receipt_type_0 import (
            TaskBatchProposeResponseReleaseReceiptType0,
        )

        success = self.success

        proposal_id: None | str | Unset
        if isinstance(self.proposal_id, Unset):
            proposal_id = UNSET
        else:
            proposal_id = self.proposal_id

        dry_run = self.dry_run

        committed = self.committed

        already_committed = self.already_committed

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

        status: None | str | Unset
        if isinstance(self.status, Unset):
            status = UNSET
        elif isinstance(self.status, TaskBatchProposeResponseStatusType0):
            status = self.status.value
        else:
            status = self.status

        batch_id: None | str | Unset
        if isinstance(self.batch_id, Unset):
            batch_id = UNSET
        else:
            batch_id = self.batch_id

        state: None | str | Unset
        if isinstance(self.state, Unset):
            state = UNSET
        else:
            state = self.state

        structure_revision: int | None | Unset
        if isinstance(self.structure_revision, Unset):
            structure_revision = UNSET
        else:
            structure_revision = self.structure_revision

        structure_digest: None | str | Unset
        if isinstance(self.structure_digest, Unset):
            structure_digest = UNSET
        else:
            structure_digest = self.structure_digest

        members: dict[str, Any] | None | Unset
        if isinstance(self.members, Unset):
            members = UNSET
        elif isinstance(self.members, TaskBatchProposeResponseMembersType0):
            members = self.members.to_dict()
        else:
            members = self.members

        lineage: dict[str, Any] | None | Unset
        if isinstance(self.lineage, Unset):
            lineage = UNSET
        elif isinstance(self.lineage, TaskBatchProposeResponseLineageType0):
            lineage = self.lineage.to_dict()
        else:
            lineage = self.lineage

        release_receipt: dict[str, Any] | None | Unset
        if isinstance(self.release_receipt, Unset):
            release_receipt = UNSET
        elif isinstance(self.release_receipt, TaskBatchProposeResponseReleaseReceiptType0):
            release_receipt = self.release_receipt.to_dict()
        else:
            release_receipt = self.release_receipt

        reviewer_task_id: None | str | Unset
        if isinstance(self.reviewer_task_id, Unset):
            reviewer_task_id = UNSET
        else:
            reviewer_task_id = self.reviewer_task_id

        replayed: bool | None | Unset
        if isinstance(self.replayed, Unset):
            replayed = UNSET
        else:
            replayed = self.replayed

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
        if already_committed is not UNSET:
            field_dict["already_committed"] = already_committed
        if task_ids is not UNSET:
            field_dict["task_ids"] = task_ids
        if diff is not UNSET:
            field_dict["diff"] = diff
        if status is not UNSET:
            field_dict["status"] = status
        if batch_id is not UNSET:
            field_dict["batch_id"] = batch_id
        if state is not UNSET:
            field_dict["state"] = state
        if structure_revision is not UNSET:
            field_dict["structure_revision"] = structure_revision
        if structure_digest is not UNSET:
            field_dict["structure_digest"] = structure_digest
        if members is not UNSET:
            field_dict["members"] = members
        if lineage is not UNSET:
            field_dict["lineage"] = lineage
        if release_receipt is not UNSET:
            field_dict["release_receipt"] = release_receipt
        if reviewer_task_id is not UNSET:
            field_dict["reviewer_task_id"] = reviewer_task_id
        if replayed is not UNSET:
            field_dict["replayed"] = replayed

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_batch_propose_response_diff_type_0 import TaskBatchProposeResponseDiffType0
        from ..models.task_batch_propose_response_lineage_type_0 import TaskBatchProposeResponseLineageType0
        from ..models.task_batch_propose_response_members_type_0 import TaskBatchProposeResponseMembersType0
        from ..models.task_batch_propose_response_release_receipt_type_0 import (
            TaskBatchProposeResponseReleaseReceiptType0,
        )

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

        already_committed = d.pop("already_committed", UNSET)

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

        def _parse_status(data: object) -> None | TaskBatchProposeResponseStatusType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                status_type_0 = TaskBatchProposeResponseStatusType0(data)

                return status_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskBatchProposeResponseStatusType0 | Unset, data)

        status = _parse_status(d.pop("status", UNSET))

        def _parse_batch_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        batch_id = _parse_batch_id(d.pop("batch_id", UNSET))

        def _parse_state(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        state = _parse_state(d.pop("state", UNSET))

        def _parse_structure_revision(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        structure_revision = _parse_structure_revision(d.pop("structure_revision", UNSET))

        def _parse_structure_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        structure_digest = _parse_structure_digest(d.pop("structure_digest", UNSET))

        def _parse_members(data: object) -> None | TaskBatchProposeResponseMembersType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                members_type_0 = TaskBatchProposeResponseMembersType0.from_dict(data)

                return members_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskBatchProposeResponseMembersType0 | Unset, data)

        members = _parse_members(d.pop("members", UNSET))

        def _parse_lineage(data: object) -> None | TaskBatchProposeResponseLineageType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                lineage_type_0 = TaskBatchProposeResponseLineageType0.from_dict(data)

                return lineage_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskBatchProposeResponseLineageType0 | Unset, data)

        lineage = _parse_lineage(d.pop("lineage", UNSET))

        def _parse_release_receipt(data: object) -> None | TaskBatchProposeResponseReleaseReceiptType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                release_receipt_type_0 = TaskBatchProposeResponseReleaseReceiptType0.from_dict(data)

                return release_receipt_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskBatchProposeResponseReleaseReceiptType0 | Unset, data)

        release_receipt = _parse_release_receipt(d.pop("release_receipt", UNSET))

        def _parse_reviewer_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reviewer_task_id = _parse_reviewer_task_id(d.pop("reviewer_task_id", UNSET))

        def _parse_replayed(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        replayed = _parse_replayed(d.pop("replayed", UNSET))

        task_batch_propose_response = cls(
            success=success,
            proposal_id=proposal_id,
            dry_run=dry_run,
            committed=committed,
            already_committed=already_committed,
            task_ids=task_ids,
            diff=diff,
            status=status,
            batch_id=batch_id,
            state=state,
            structure_revision=structure_revision,
            structure_digest=structure_digest,
            members=members,
            lineage=lineage,
            release_receipt=release_receipt,
            reviewer_task_id=reviewer_task_id,
            replayed=replayed,
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
