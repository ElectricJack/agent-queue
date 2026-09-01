from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.get_task_tree_response_root import GetTaskTreeResponseRoot
    from ..models.get_task_tree_response_subtask_by_status import GetTaskTreeResponseSubtaskByStatus


T = TypeVar("T", bound="GetTaskTreeResponse")


@_attrs_define
class GetTaskTreeResponse:
    """
    Attributes:
        root (GetTaskTreeResponseRoot | Unset):
        formatted (str | Unset):  Default: ''.
        subtask_completed (int | Unset):  Default: 0.
        subtask_total (int | Unset):  Default: 0.
        subtask_by_status (GetTaskTreeResponseSubtaskByStatus | Unset):
        progress_bar (None | str | Unset):
    """

    root: GetTaskTreeResponseRoot | Unset = UNSET
    formatted: str | Unset = ""
    subtask_completed: int | Unset = 0
    subtask_total: int | Unset = 0
    subtask_by_status: GetTaskTreeResponseSubtaskByStatus | Unset = UNSET
    progress_bar: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        root: dict[str, Any] | Unset = UNSET
        if not isinstance(self.root, Unset):
            root = self.root.to_dict()

        formatted = self.formatted

        subtask_completed = self.subtask_completed

        subtask_total = self.subtask_total

        subtask_by_status: dict[str, Any] | Unset = UNSET
        if not isinstance(self.subtask_by_status, Unset):
            subtask_by_status = self.subtask_by_status.to_dict()

        progress_bar: None | str | Unset
        if isinstance(self.progress_bar, Unset):
            progress_bar = UNSET
        else:
            progress_bar = self.progress_bar

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if root is not UNSET:
            field_dict["root"] = root
        if formatted is not UNSET:
            field_dict["formatted"] = formatted
        if subtask_completed is not UNSET:
            field_dict["subtask_completed"] = subtask_completed
        if subtask_total is not UNSET:
            field_dict["subtask_total"] = subtask_total
        if subtask_by_status is not UNSET:
            field_dict["subtask_by_status"] = subtask_by_status
        if progress_bar is not UNSET:
            field_dict["progress_bar"] = progress_bar

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.get_task_tree_response_root import GetTaskTreeResponseRoot
        from ..models.get_task_tree_response_subtask_by_status import GetTaskTreeResponseSubtaskByStatus

        d = dict(src_dict)
        _root = d.pop("root", UNSET)
        root: GetTaskTreeResponseRoot | Unset
        if isinstance(_root, Unset):
            root = UNSET
        else:
            root = GetTaskTreeResponseRoot.from_dict(_root)

        formatted = d.pop("formatted", UNSET)

        subtask_completed = d.pop("subtask_completed", UNSET)

        subtask_total = d.pop("subtask_total", UNSET)

        _subtask_by_status = d.pop("subtask_by_status", UNSET)
        subtask_by_status: GetTaskTreeResponseSubtaskByStatus | Unset
        if isinstance(_subtask_by_status, Unset):
            subtask_by_status = UNSET
        else:
            subtask_by_status = GetTaskTreeResponseSubtaskByStatus.from_dict(_subtask_by_status)

        def _parse_progress_bar(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        progress_bar = _parse_progress_bar(d.pop("progress_bar", UNSET))

        get_task_tree_response = cls(
            root=root,
            formatted=formatted,
            subtask_completed=subtask_completed,
            subtask_total=subtask_total,
            subtask_by_status=subtask_by_status,
            progress_bar=progress_bar,
        )

        get_task_tree_response.additional_properties = d
        return get_task_tree_response

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
