from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.select_files_for_inspection_request_weights_type_0 import SelectFilesForInspectionRequestWeightsType0


T = TypeVar("T", bound="SelectFilesForInspectionRequest")


@_attrs_define
class SelectFilesForInspectionRequest:
    """
    Attributes:
        project_id (str): Project ID whose workspace to enumerate. Falls back to the active project if omitted.
        workspace (None | str | Unset): Workspace name or ID (default: first workspace)
        count (int | Unset): Total number of files to select (default 5). Default: 5.
        weights (None | SelectFilesForInspectionRequestWeightsType0 | Unset): Optional weighted distribution across
            categories. Defaults to the codebase-inspector spec: {source: 0.40, specs: 0.20, tests: 0.15, config: 0.10,
            recent: 0.15}. Values are normalized.
        recent_days (int | Unset): Files modified within this many days are eligible for the 'recent' category (default
            7). Default: 7.
        history_lookback_days (int | Unset): Exclude files that were inspected within this window, based on project-
            memory inspection records (default 21). Set to 0 to disable. Default: 21.
        seed (int | None | Unset): Optional RNG seed for deterministic selection (useful for tests and reproducible
            runs).
    """

    project_id: str
    workspace: None | str | Unset = UNSET
    count: int | Unset = 5
    weights: None | SelectFilesForInspectionRequestWeightsType0 | Unset = UNSET
    recent_days: int | Unset = 7
    history_lookback_days: int | Unset = 21
    seed: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.select_files_for_inspection_request_weights_type_0 import (
            SelectFilesForInspectionRequestWeightsType0,
        )

        project_id = self.project_id

        workspace: None | str | Unset
        if isinstance(self.workspace, Unset):
            workspace = UNSET
        else:
            workspace = self.workspace

        count = self.count

        weights: dict[str, Any] | None | Unset
        if isinstance(self.weights, Unset):
            weights = UNSET
        elif isinstance(self.weights, SelectFilesForInspectionRequestWeightsType0):
            weights = self.weights.to_dict()
        else:
            weights = self.weights

        recent_days = self.recent_days

        history_lookback_days = self.history_lookback_days

        seed: int | None | Unset
        if isinstance(self.seed, Unset):
            seed = UNSET
        else:
            seed = self.seed

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "project_id": project_id,
            }
        )
        if workspace is not UNSET:
            field_dict["workspace"] = workspace
        if count is not UNSET:
            field_dict["count"] = count
        if weights is not UNSET:
            field_dict["weights"] = weights
        if recent_days is not UNSET:
            field_dict["recent_days"] = recent_days
        if history_lookback_days is not UNSET:
            field_dict["history_lookback_days"] = history_lookback_days
        if seed is not UNSET:
            field_dict["seed"] = seed

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.select_files_for_inspection_request_weights_type_0 import (
            SelectFilesForInspectionRequestWeightsType0,
        )

        d = dict(src_dict)
        project_id = d.pop("project_id")

        def _parse_workspace(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        workspace = _parse_workspace(d.pop("workspace", UNSET))

        count = d.pop("count", UNSET)

        def _parse_weights(data: object) -> None | SelectFilesForInspectionRequestWeightsType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                weights_type_0 = SelectFilesForInspectionRequestWeightsType0.from_dict(data)

                return weights_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SelectFilesForInspectionRequestWeightsType0 | Unset, data)

        weights = _parse_weights(d.pop("weights", UNSET))

        recent_days = d.pop("recent_days", UNSET)

        history_lookback_days = d.pop("history_lookback_days", UNSET)

        def _parse_seed(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        seed = _parse_seed(d.pop("seed", UNSET))

        select_files_for_inspection_request = cls(
            project_id=project_id,
            workspace=workspace,
            count=count,
            weights=weights,
            recent_days=recent_days,
            history_lookback_days=history_lookback_days,
            seed=seed,
        )

        select_files_for_inspection_request.additional_properties = d
        return select_files_for_inspection_request

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
