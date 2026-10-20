from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.archive_task_request_rewire_type_0 import ArchiveTaskRequestRewireType0


T = TypeVar("T", bound="ArchiveTaskRequest")


@_attrs_define
class ArchiveTaskRequest:
    """
    Attributes:
        task_id (None | str | Unset): Archive a single task by ID; PAUSED, DEFINED and READY require reason
        project_id (None | str | Unset): Bulk-archive all completed tasks in this project (alternative to task_id)
        include_failed (bool | None | Unset): When bulk-archiving by project_id, also archive FAILED and BLOCKED tasks.
            Default false.
        abandon_undelivered (bool | Unset): Single-task archive only: explicitly abandon completed work that has not
            reached the default branch. Requires disposition, reason and an elevated/operator caller. Default: False.
        disposition (None | str | Unset): Single-task only: what happens to completed work that has not reached the
            default branch. deliver keeps the task for the train and reports what its delivery waits on; obsolete (reason is
            the note) and retire archive it, retiring its branches after a backup bundle.
        reason (None | str | Unset): Required to archive PAUSED/DEFINED/READY or abandon undelivered work.
        replacement (None | str | Unset): Obsolete only: replacement leaf; atomically redirects dependents.
        satisfied_by (None | str | Unset): Obsolete only: evidence the requirement is met; dependent edges must be
            explicitly removed or replaced through rewire. Never records a pass.
        rewire (ArchiveTaskRequestRewireType0 | None | Unset): Obsolete edge plan, validated and committed with
            retirement. Uses task change-set edges/remove_edges objects (from, to, dep_type).
    """

    task_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    include_failed: bool | None | Unset = UNSET
    abandon_undelivered: bool | Unset = False
    disposition: None | str | Unset = UNSET
    reason: None | str | Unset = UNSET
    replacement: None | str | Unset = UNSET
    satisfied_by: None | str | Unset = UNSET
    rewire: ArchiveTaskRequestRewireType0 | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.archive_task_request_rewire_type_0 import ArchiveTaskRequestRewireType0

        task_id: None | str | Unset
        if isinstance(self.task_id, Unset):
            task_id = UNSET
        else:
            task_id = self.task_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        include_failed: bool | None | Unset
        if isinstance(self.include_failed, Unset):
            include_failed = UNSET
        else:
            include_failed = self.include_failed

        abandon_undelivered = self.abandon_undelivered

        disposition: None | str | Unset
        if isinstance(self.disposition, Unset):
            disposition = UNSET
        else:
            disposition = self.disposition

        reason: None | str | Unset
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason

        replacement: None | str | Unset
        if isinstance(self.replacement, Unset):
            replacement = UNSET
        else:
            replacement = self.replacement

        satisfied_by: None | str | Unset
        if isinstance(self.satisfied_by, Unset):
            satisfied_by = UNSET
        else:
            satisfied_by = self.satisfied_by

        rewire: dict[str, Any] | None | Unset
        if isinstance(self.rewire, Unset):
            rewire = UNSET
        elif isinstance(self.rewire, ArchiveTaskRequestRewireType0):
            rewire = self.rewire.to_dict()
        else:
            rewire = self.rewire

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if include_failed is not UNSET:
            field_dict["include_failed"] = include_failed
        if abandon_undelivered is not UNSET:
            field_dict["abandon_undelivered"] = abandon_undelivered
        if disposition is not UNSET:
            field_dict["disposition"] = disposition
        if reason is not UNSET:
            field_dict["reason"] = reason
        if replacement is not UNSET:
            field_dict["replacement"] = replacement
        if satisfied_by is not UNSET:
            field_dict["satisfied_by"] = satisfied_by
        if rewire is not UNSET:
            field_dict["rewire"] = rewire

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.archive_task_request_rewire_type_0 import ArchiveTaskRequestRewireType0

        d = dict(src_dict)

        def _parse_task_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        task_id = _parse_task_id(d.pop("task_id", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_include_failed(data: object) -> bool | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | None | Unset, data)

        include_failed = _parse_include_failed(d.pop("include_failed", UNSET))

        abandon_undelivered = d.pop("abandon_undelivered", UNSET)

        def _parse_disposition(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        disposition = _parse_disposition(d.pop("disposition", UNSET))

        def _parse_reason(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        reason = _parse_reason(d.pop("reason", UNSET))

        def _parse_replacement(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        replacement = _parse_replacement(d.pop("replacement", UNSET))

        def _parse_satisfied_by(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        satisfied_by = _parse_satisfied_by(d.pop("satisfied_by", UNSET))

        def _parse_rewire(data: object) -> ArchiveTaskRequestRewireType0 | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                rewire_type_0 = ArchiveTaskRequestRewireType0.from_dict(data)

                return rewire_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ArchiveTaskRequestRewireType0 | None | Unset, data)

        rewire = _parse_rewire(d.pop("rewire", UNSET))

        archive_task_request = cls(
            task_id=task_id,
            project_id=project_id,
            include_failed=include_failed,
            abandon_undelivered=abandon_undelivered,
            disposition=disposition,
            reason=reason,
            replacement=replacement,
            satisfied_by=satisfied_by,
            rewire=rewire,
        )

        archive_task_request.additional_properties = d
        return archive_task_request

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
