from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.report_brief_response_active_item import ReportBriefResponseActiveItem
    from ..models.report_brief_response_brief import ReportBriefResponseBrief
    from ..models.report_brief_response_facts_item import ReportBriefResponseFactsItem


T = TypeVar("T", bound="ReportBriefResponse")


@_attrs_define
class ReportBriefResponse:
    """
    Attributes:
        request_id (str):
        state (str):
        deadline (float):
        version (int):
        brief_hash (str):
        brief (ReportBriefResponseBrief):
        facts (list[ReportBriefResponseFactsItem]):
        active (list[ReportBriefResponseActiveItem]):
        total_facts (int):
        total_active (int):
        success (bool | Unset):  Default: True.
    """

    request_id: str
    state: str
    deadline: float
    version: int
    brief_hash: str
    brief: ReportBriefResponseBrief
    facts: list[ReportBriefResponseFactsItem]
    active: list[ReportBriefResponseActiveItem]
    total_facts: int
    total_active: int
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        request_id = self.request_id

        state = self.state

        deadline = self.deadline

        version = self.version

        brief_hash = self.brief_hash

        brief = self.brief.to_dict()

        facts = []
        for facts_item_data in self.facts:
            facts_item = facts_item_data.to_dict()
            facts.append(facts_item)

        active = []
        for active_item_data in self.active:
            active_item = active_item_data.to_dict()
            active.append(active_item)

        total_facts = self.total_facts

        total_active = self.total_active

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "request_id": request_id,
                "state": state,
                "deadline": deadline,
                "version": version,
                "brief_hash": brief_hash,
                "brief": brief,
                "facts": facts,
                "active": active,
                "total_facts": total_facts,
                "total_active": total_active,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.report_brief_response_active_item import ReportBriefResponseActiveItem
        from ..models.report_brief_response_brief import ReportBriefResponseBrief
        from ..models.report_brief_response_facts_item import ReportBriefResponseFactsItem

        d = dict(src_dict)
        request_id = d.pop("request_id")

        state = d.pop("state")

        deadline = d.pop("deadline")

        version = d.pop("version")

        brief_hash = d.pop("brief_hash")

        brief = ReportBriefResponseBrief.from_dict(d.pop("brief"))

        facts = []
        _facts = d.pop("facts")
        for facts_item_data in _facts:
            facts_item = ReportBriefResponseFactsItem.from_dict(facts_item_data)

            facts.append(facts_item)

        active = []
        _active = d.pop("active")
        for active_item_data in _active:
            active_item = ReportBriefResponseActiveItem.from_dict(active_item_data)

            active.append(active_item)

        total_facts = d.pop("total_facts")

        total_active = d.pop("total_active")

        success = d.pop("success", UNSET)

        report_brief_response = cls(
            request_id=request_id,
            state=state,
            deadline=deadline,
            version=version,
            brief_hash=brief_hash,
            brief=brief,
            facts=facts,
            active=active,
            total_facts=total_facts,
            total_active=total_active,
            success=success,
        )

        report_brief_response.additional_properties = d
        return report_brief_response

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
