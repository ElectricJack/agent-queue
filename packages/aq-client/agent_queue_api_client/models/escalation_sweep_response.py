from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.escalation_sweep_response_plan import EscalationSweepResponsePlan
    from ..models.escalation_sweep_response_report import EscalationSweepResponseReport


T = TypeVar("T", bound="EscalationSweepResponse")


@_attrs_define
class EscalationSweepResponse:
    """One §5.6 sweep pass: the plan, and what the pass did about it.

    Attributes:
        mode (str):
        plan (EscalationSweepResponsePlan):
        report (EscalationSweepResponseReport):
        open_before (int):
        open_after (int):
        target_open_items (int):
        within_target (bool):
        success (bool | Unset):  Default: True.
    """

    mode: str
    plan: EscalationSweepResponsePlan
    report: EscalationSweepResponseReport
    open_before: int
    open_after: int
    target_open_items: int
    within_target: bool
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        mode = self.mode

        plan = self.plan.to_dict()

        report = self.report.to_dict()

        open_before = self.open_before

        open_after = self.open_after

        target_open_items = self.target_open_items

        within_target = self.within_target

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "mode": mode,
                "plan": plan,
                "report": report,
                "open_before": open_before,
                "open_after": open_after,
                "target_open_items": target_open_items,
                "within_target": within_target,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.escalation_sweep_response_plan import EscalationSweepResponsePlan
        from ..models.escalation_sweep_response_report import EscalationSweepResponseReport

        d = dict(src_dict)
        mode = d.pop("mode")

        plan = EscalationSweepResponsePlan.from_dict(d.pop("plan"))

        report = EscalationSweepResponseReport.from_dict(d.pop("report"))

        open_before = d.pop("open_before")

        open_after = d.pop("open_after")

        target_open_items = d.pop("target_open_items")

        within_target = d.pop("within_target")

        success = d.pop("success", UNSET)

        escalation_sweep_response = cls(
            mode=mode,
            plan=plan,
            report=report,
            open_before=open_before,
            open_after=open_after,
            target_open_items=target_open_items,
            within_target=within_target,
            success=success,
        )

        escalation_sweep_response.additional_properties = d
        return escalation_sweep_response

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
