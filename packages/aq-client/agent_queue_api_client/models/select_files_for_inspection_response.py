from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.select_files_for_inspection_response_categorized import SelectFilesForInspectionResponseCategorized
    from ..models.select_files_for_inspection_response_target_counts import SelectFilesForInspectionResponseTargetCounts
    from ..models.select_files_for_inspection_response_weights import SelectFilesForInspectionResponseWeights


T = TypeVar("T", bound="SelectFilesForInspectionResponse")


@_attrs_define
class SelectFilesForInspectionResponse:
    """
    Attributes:
        project_id (str):
        workspace_name (str | Unset):  Default: ''.
        workspace_path (str | Unset):  Default: ''.
        files (list[str] | Unset):
        categorized (SelectFilesForInspectionResponseCategorized | Unset):
        weights (SelectFilesForInspectionResponseWeights | Unset):
        target_counts (SelectFilesForInspectionResponseTargetCounts | Unset):
        total_enumerated (int | Unset):  Default: 0.
        excluded_history (int | Unset):  Default: 0.
        history_files (list[str] | Unset):
        history_lookback_days (int | Unset):  Default: 0.
    """

    project_id: str
    workspace_name: str | Unset = ""
    workspace_path: str | Unset = ""
    files: list[str] | Unset = UNSET
    categorized: SelectFilesForInspectionResponseCategorized | Unset = UNSET
    weights: SelectFilesForInspectionResponseWeights | Unset = UNSET
    target_counts: SelectFilesForInspectionResponseTargetCounts | Unset = UNSET
    total_enumerated: int | Unset = 0
    excluded_history: int | Unset = 0
    history_files: list[str] | Unset = UNSET
    history_lookback_days: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id = self.project_id

        workspace_name = self.workspace_name

        workspace_path = self.workspace_path

        files: list[str] | Unset = UNSET
        if not isinstance(self.files, Unset):
            files = self.files

        categorized: dict[str, Any] | Unset = UNSET
        if not isinstance(self.categorized, Unset):
            categorized = self.categorized.to_dict()

        weights: dict[str, Any] | Unset = UNSET
        if not isinstance(self.weights, Unset):
            weights = self.weights.to_dict()

        target_counts: dict[str, Any] | Unset = UNSET
        if not isinstance(self.target_counts, Unset):
            target_counts = self.target_counts.to_dict()

        total_enumerated = self.total_enumerated

        excluded_history = self.excluded_history

        history_files: list[str] | Unset = UNSET
        if not isinstance(self.history_files, Unset):
            history_files = self.history_files

        history_lookback_days = self.history_lookback_days

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if workspace_name is not UNSET:
            field_dict["workspace_name"] = workspace_name
        if workspace_path is not UNSET:
            field_dict["workspace_path"] = workspace_path
        if files is not UNSET:
            field_dict["files"] = files
        if categorized is not UNSET:
            field_dict["categorized"] = categorized
        if weights is not UNSET:
            field_dict["weights"] = weights
        if target_counts is not UNSET:
            field_dict["target_counts"] = target_counts
        if total_enumerated is not UNSET:
            field_dict["total_enumerated"] = total_enumerated
        if excluded_history is not UNSET:
            field_dict["excluded_history"] = excluded_history
        if history_files is not UNSET:
            field_dict["history_files"] = history_files
        if history_lookback_days is not UNSET:
            field_dict["history_lookback_days"] = history_lookback_days

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.select_files_for_inspection_response_categorized import (
            SelectFilesForInspectionResponseCategorized,
        )
        from ..models.select_files_for_inspection_response_target_counts import (
            SelectFilesForInspectionResponseTargetCounts,
        )
        from ..models.select_files_for_inspection_response_weights import SelectFilesForInspectionResponseWeights

        d = dict(src_dict)
        project_id = d.pop("project_id")

        workspace_name = d.pop("workspace_name", UNSET)

        workspace_path = d.pop("workspace_path", UNSET)

        files = cast(list[str], d.pop("files", UNSET))

        _categorized = d.pop("categorized", UNSET)
        categorized: SelectFilesForInspectionResponseCategorized | Unset
        if isinstance(_categorized, Unset):
            categorized = UNSET
        else:
            categorized = SelectFilesForInspectionResponseCategorized.from_dict(_categorized)

        _weights = d.pop("weights", UNSET)
        weights: SelectFilesForInspectionResponseWeights | Unset
        if isinstance(_weights, Unset):
            weights = UNSET
        else:
            weights = SelectFilesForInspectionResponseWeights.from_dict(_weights)

        _target_counts = d.pop("target_counts", UNSET)
        target_counts: SelectFilesForInspectionResponseTargetCounts | Unset
        if isinstance(_target_counts, Unset):
            target_counts = UNSET
        else:
            target_counts = SelectFilesForInspectionResponseTargetCounts.from_dict(_target_counts)

        total_enumerated = d.pop("total_enumerated", UNSET)

        excluded_history = d.pop("excluded_history", UNSET)

        history_files = cast(list[str], d.pop("history_files", UNSET))

        history_lookback_days = d.pop("history_lookback_days", UNSET)

        select_files_for_inspection_response = cls(
            project_id=project_id,
            workspace_name=workspace_name,
            workspace_path=workspace_path,
            files=files,
            categorized=categorized,
            weights=weights,
            target_counts=target_counts,
            total_enumerated=total_enumerated,
            excluded_history=excluded_history,
            history_files=history_files,
            history_lookback_days=history_lookback_days,
        )

        select_files_for_inspection_response.additional_properties = d
        return select_files_for_inspection_response

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
