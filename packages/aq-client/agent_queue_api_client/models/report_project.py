from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.manual_check import ManualCheck
    from ..models.report_item import ReportItem


T = TypeVar("T", bound="ReportProject")


@_attrs_define
class ReportProject:
    """
    Attributes:
        id (str):
        landed (list[ReportItem]):
        pending (list[ReportItem]):
        failures (list[ReportItem]):
        manual_checks (list[ManualCheck]):
        name (str | Unset):  Default: ''.
    """

    id: str
    landed: list[ReportItem]
    pending: list[ReportItem]
    failures: list[ReportItem]
    manual_checks: list[ManualCheck]
    name: str | Unset = ""
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        id = self.id

        landed = []
        for landed_item_data in self.landed:
            landed_item = landed_item_data.to_dict()
            landed.append(landed_item)

        pending = []
        for pending_item_data in self.pending:
            pending_item = pending_item_data.to_dict()
            pending.append(pending_item)

        failures = []
        for failures_item_data in self.failures:
            failures_item = failures_item_data.to_dict()
            failures.append(failures_item)

        manual_checks = []
        for manual_checks_item_data in self.manual_checks:
            manual_checks_item = manual_checks_item_data.to_dict()
            manual_checks.append(manual_checks_item)

        name = self.name

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "landed": landed,
                "pending": pending,
                "failures": failures,
                "manual_checks": manual_checks,
            }
        )
        if name is not UNSET:
            field_dict["name"] = name

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.manual_check import ManualCheck
        from ..models.report_item import ReportItem

        d = dict(src_dict)
        id = d.pop("id")

        landed = []
        _landed = d.pop("landed")
        for landed_item_data in _landed:
            landed_item = ReportItem.from_dict(landed_item_data)

            landed.append(landed_item)

        pending = []
        _pending = d.pop("pending")
        for pending_item_data in _pending:
            pending_item = ReportItem.from_dict(pending_item_data)

            pending.append(pending_item)

        failures = []
        _failures = d.pop("failures")
        for failures_item_data in _failures:
            failures_item = ReportItem.from_dict(failures_item_data)

            failures.append(failures_item)

        manual_checks = []
        _manual_checks = d.pop("manual_checks")
        for manual_checks_item_data in _manual_checks:
            manual_checks_item = ManualCheck.from_dict(manual_checks_item_data)

            manual_checks.append(manual_checks_item)

        name = d.pop("name", UNSET)

        report_project = cls(
            id=id,
            landed=landed,
            pending=pending,
            failures=failures,
            manual_checks=manual_checks,
            name=name,
        )

        report_project.additional_properties = d
        return report_project

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
