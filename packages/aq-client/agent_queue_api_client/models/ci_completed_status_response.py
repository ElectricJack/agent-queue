from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ci_completed_status_response_failing_item import CiCompletedStatusResponseFailingItem
    from ..models.ci_completed_status_response_unresolved_item import CiCompletedStatusResponseUnresolvedItem


T = TypeVar("T", bound="CiCompletedStatusResponse")


@_attrs_define
class CiCompletedStatusResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        project_id (str | Unset):  Default: ''.
        checked (int | Unset):  Default: 0.
        failing (list[CiCompletedStatusResponseFailingItem] | Unset):
        unresolved (list[CiCompletedStatusResponseUnresolvedItem] | Unset):
        error (None | str | Unset):
    """

    success: bool | Unset = True
    project_id: str | Unset = ""
    checked: int | Unset = 0
    failing: list[CiCompletedStatusResponseFailingItem] | Unset = UNSET
    unresolved: list[CiCompletedStatusResponseUnresolvedItem] | Unset = UNSET
    error: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        project_id = self.project_id

        checked = self.checked

        failing: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.failing, Unset):
            failing = []
            for failing_item_data in self.failing:
                failing_item = failing_item_data.to_dict()
                failing.append(failing_item)

        unresolved: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.unresolved, Unset):
            unresolved = []
            for unresolved_item_data in self.unresolved:
                unresolved_item = unresolved_item_data.to_dict()
                unresolved.append(unresolved_item)

        error: None | str | Unset
        if isinstance(self.error, Unset):
            error = UNSET
        else:
            error = self.error

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if checked is not UNSET:
            field_dict["checked"] = checked
        if failing is not UNSET:
            field_dict["failing"] = failing
        if unresolved is not UNSET:
            field_dict["unresolved"] = unresolved
        if error is not UNSET:
            field_dict["error"] = error

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ci_completed_status_response_failing_item import CiCompletedStatusResponseFailingItem
        from ..models.ci_completed_status_response_unresolved_item import CiCompletedStatusResponseUnresolvedItem

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        project_id = d.pop("project_id", UNSET)

        checked = d.pop("checked", UNSET)

        _failing = d.pop("failing", UNSET)
        failing: list[CiCompletedStatusResponseFailingItem] | Unset = UNSET
        if _failing is not UNSET:
            failing = []
            for failing_item_data in _failing:
                failing_item = CiCompletedStatusResponseFailingItem.from_dict(failing_item_data)

                failing.append(failing_item)

        _unresolved = d.pop("unresolved", UNSET)
        unresolved: list[CiCompletedStatusResponseUnresolvedItem] | Unset = UNSET
        if _unresolved is not UNSET:
            unresolved = []
            for unresolved_item_data in _unresolved:
                unresolved_item = CiCompletedStatusResponseUnresolvedItem.from_dict(unresolved_item_data)

                unresolved.append(unresolved_item)

        def _parse_error(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        error = _parse_error(d.pop("error", UNSET))

        ci_completed_status_response = cls(
            success=success,
            project_id=project_id,
            checked=checked,
            failing=failing,
            unresolved=unresolved,
            error=error,
        )

        ci_completed_status_response.additional_properties = d
        return ci_completed_status_response

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
