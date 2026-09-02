from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.get_token_usage_response_breakdown_item import GetTokenUsageResponseBreakdownItem


T = TypeVar("T", bound="GetTokenUsageResponse")


@_attrs_define
class GetTokenUsageResponse:
    """
    Attributes:
        task_id (None | str | Unset):
        project_id (None | str | Unset):
        breakdown (list[GetTokenUsageResponseBreakdownItem] | Unset):
        total (int | Unset):  Default: 0.
    """

    task_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    breakdown: list[GetTokenUsageResponseBreakdownItem] | Unset = UNSET
    total: int | Unset = 0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
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

        breakdown: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.breakdown, Unset):
            breakdown = []
            for breakdown_item_data in self.breakdown:
                breakdown_item = breakdown_item_data.to_dict()
                breakdown.append(breakdown_item)

        total = self.total

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if task_id is not UNSET:
            field_dict["task_id"] = task_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if breakdown is not UNSET:
            field_dict["breakdown"] = breakdown
        if total is not UNSET:
            field_dict["total"] = total

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.get_token_usage_response_breakdown_item import GetTokenUsageResponseBreakdownItem

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

        _breakdown = d.pop("breakdown", UNSET)
        breakdown: list[GetTokenUsageResponseBreakdownItem] | Unset = UNSET
        if _breakdown is not UNSET:
            breakdown = []
            for breakdown_item_data in _breakdown:
                breakdown_item = GetTokenUsageResponseBreakdownItem.from_dict(breakdown_item_data)

                breakdown.append(breakdown_item)

        total = d.pop("total", UNSET)

        get_token_usage_response = cls(
            task_id=task_id,
            project_id=project_id,
            breakdown=breakdown,
            total=total,
        )

        get_token_usage_response.additional_properties = d
        return get_token_usage_response

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
