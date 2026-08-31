from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.task_set_request_meta_type_0 import TaskSetRequestMetaType0


T = TypeVar("T", bound="TaskSetRequest")


@_attrs_define
class TaskSetRequest:
    """
    Attributes:
        task_id (str): Task id to update.
        description (None | str | Unset): Replace the canonical task description, preserving requirements and adding
            durable findings.
        expected_description (None | str | Unset): Exact description previously read; rejects concurrent edits before
            changing any fields.
        branch (None | str | Unset): Branch name for this task's work (optional).
        pr_url (None | str | Unset): Pull-request URL (optional).
        work_dir (None | str | Unset): Directory the work happens in (optional; recorded as task metadata).
        note (None | str | Unset): Free-text note appended to the task's context (optional).
        labels_add (list[Any] | None | Unset): Labels to add (optional).
        labels_remove (list[Any] | None | Unset): Labels to remove (optional).
        meta (None | TaskSetRequestMetaType0 | Unset): Arbitrary key/value task metadata to set (optional).
        claim_epoch (int | None | Unset): Current claim epoch for a pool-session caller (optional — the CLI reads it
            from .aq/claim.json).
    """

    task_id: str
    description: None | str | Unset = UNSET
    expected_description: None | str | Unset = UNSET
    branch: None | str | Unset = UNSET
    pr_url: None | str | Unset = UNSET
    work_dir: None | str | Unset = UNSET
    note: None | str | Unset = UNSET
    labels_add: list[Any] | None | Unset = UNSET
    labels_remove: list[Any] | None | Unset = UNSET
    meta: None | TaskSetRequestMetaType0 | Unset = UNSET
    claim_epoch: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.task_set_request_meta_type_0 import TaskSetRequestMetaType0  # noqa: PLC0415

        task_id = self.task_id

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        expected_description: None | str | Unset
        if isinstance(self.expected_description, Unset):
            expected_description = UNSET
        else:
            expected_description = self.expected_description

        branch: None | str | Unset
        if isinstance(self.branch, Unset):
            branch = UNSET
        else:
            branch = self.branch

        pr_url: None | str | Unset
        if isinstance(self.pr_url, Unset):
            pr_url = UNSET
        else:
            pr_url = self.pr_url

        work_dir: None | str | Unset
        if isinstance(self.work_dir, Unset):
            work_dir = UNSET
        else:
            work_dir = self.work_dir

        note: None | str | Unset
        if isinstance(self.note, Unset):
            note = UNSET
        else:
            note = self.note

        labels_add: list[Any] | None | Unset
        if isinstance(self.labels_add, Unset):
            labels_add = UNSET
        elif isinstance(self.labels_add, list):
            labels_add = self.labels_add

        else:
            labels_add = self.labels_add

        labels_remove: list[Any] | None | Unset
        if isinstance(self.labels_remove, Unset):
            labels_remove = UNSET
        elif isinstance(self.labels_remove, list):
            labels_remove = self.labels_remove

        else:
            labels_remove = self.labels_remove

        meta: dict[str, Any] | None | Unset
        if isinstance(self.meta, Unset):
            meta = UNSET
        elif isinstance(self.meta, TaskSetRequestMetaType0):
            meta = self.meta.to_dict()
        else:
            meta = self.meta

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if expected_description is not UNSET:
            field_dict["expected_description"] = expected_description
        if branch is not UNSET:
            field_dict["branch"] = branch
        if pr_url is not UNSET:
            field_dict["pr_url"] = pr_url
        if work_dir is not UNSET:
            field_dict["work_dir"] = work_dir
        if note is not UNSET:
            field_dict["note"] = note
        if labels_add is not UNSET:
            field_dict["labels_add"] = labels_add
        if labels_remove is not UNSET:
            field_dict["labels_remove"] = labels_remove
        if meta is not UNSET:
            field_dict["meta"] = meta
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.task_set_request_meta_type_0 import TaskSetRequestMetaType0  # noqa: PLC0415

        d = dict(src_dict)
        task_id = d.pop("task_id")

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        def _parse_expected_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        expected_description = _parse_expected_description(d.pop("expected_description", UNSET))

        def _parse_branch(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        branch = _parse_branch(d.pop("branch", UNSET))

        def _parse_pr_url(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        pr_url = _parse_pr_url(d.pop("pr_url", UNSET))

        def _parse_work_dir(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        work_dir = _parse_work_dir(d.pop("work_dir", UNSET))

        def _parse_note(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        note = _parse_note(d.pop("note", UNSET))

        def _parse_labels_add(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                labels_add_type_0 = cast(list[Any], data)

                return labels_add_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        labels_add = _parse_labels_add(d.pop("labels_add", UNSET))

        def _parse_labels_remove(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                labels_remove_type_0 = cast(list[Any], data)

                return labels_remove_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        labels_remove = _parse_labels_remove(d.pop("labels_remove", UNSET))

        def _parse_meta(data: object) -> None | TaskSetRequestMetaType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                meta_type_0 = TaskSetRequestMetaType0.from_dict(data)

                return meta_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | TaskSetRequestMetaType0 | Unset, data)

        meta = _parse_meta(d.pop("meta", UNSET))

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        task_set_request = cls(
            task_id=task_id,
            description=description,
            expected_description=expected_description,
            branch=branch,
            pr_url=pr_url,
            work_dir=work_dir,
            note=note,
            labels_add=labels_add,
            labels_remove=labels_remove,
            meta=meta,
            claim_epoch=claim_epoch,
        )

        task_set_request.additional_properties = d
        return task_set_request

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
