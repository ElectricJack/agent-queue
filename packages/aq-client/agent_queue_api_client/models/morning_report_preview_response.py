from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.morning_report_preview_response_brief import MorningReportPreviewResponseBrief


T = TypeVar("T", bound="MorningReportPreviewResponse")


@_attrs_define
class MorningReportPreviewResponse:
    """
    Attributes:
        brief (MorningReportPreviewResponseBrief):
        brief_hash (str):
        would_suppress (bool):
        reason (str):
        success (bool | Unset):  Default: True.
    """

    brief: MorningReportPreviewResponseBrief
    brief_hash: str
    would_suppress: bool
    reason: str
    success: bool | Unset = True
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        brief = self.brief.to_dict()

        brief_hash = self.brief_hash

        would_suppress = self.would_suppress

        reason = self.reason

        success = self.success

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "brief": brief,
                "brief_hash": brief_hash,
                "would_suppress": would_suppress,
                "reason": reason,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.morning_report_preview_response_brief import MorningReportPreviewResponseBrief

        d = dict(src_dict)
        brief = MorningReportPreviewResponseBrief.from_dict(d.pop("brief"))

        brief_hash = d.pop("brief_hash")

        would_suppress = d.pop("would_suppress")

        reason = d.pop("reason")

        success = d.pop("success", UNSET)

        morning_report_preview_response = cls(
            brief=brief,
            brief_hash=brief_hash,
            would_suppress=would_suppress,
            reason=reason,
            success=success,
        )

        morning_report_preview_response.additional_properties = d
        return morning_report_preview_response

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
