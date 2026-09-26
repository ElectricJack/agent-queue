from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

if TYPE_CHECKING:
    from ..models.report_content import ReportContent


T = TypeVar("T", bound="MorningReportRecord")


@_attrs_define
class MorningReportRecord:
    """
    Attributes:
        id (str):
        state (str):
        reason (None | str):
        local_date (str):
        timezone (str):
        planned_at (float):
        window_start (float):
        window_end (float):
        brief_hash (None | str):
        created_at (float):
        finalized_at (float | None):
        author_deadline (float):
        report (None | ReportContent):
        is_fallback (bool):
    """

    id: str
    state: str
    reason: None | str
    local_date: str
    timezone: str
    planned_at: float
    window_start: float
    window_end: float
    brief_hash: None | str
    created_at: float
    finalized_at: float | None
    author_deadline: float
    report: None | ReportContent
    is_fallback: bool
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.report_content import ReportContent

        id = self.id

        state = self.state

        reason: None | str
        reason = self.reason

        local_date = self.local_date

        timezone = self.timezone

        planned_at = self.planned_at

        window_start = self.window_start

        window_end = self.window_end

        brief_hash: None | str
        brief_hash = self.brief_hash

        created_at = self.created_at

        finalized_at: float | None
        finalized_at = self.finalized_at

        author_deadline = self.author_deadline

        report: dict[str, Any] | None
        if isinstance(self.report, ReportContent):
            report = self.report.to_dict()
        else:
            report = self.report

        is_fallback = self.is_fallback

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "id": id,
                "state": state,
                "reason": reason,
                "local_date": local_date,
                "timezone": timezone,
                "planned_at": planned_at,
                "window_start": window_start,
                "window_end": window_end,
                "brief_hash": brief_hash,
                "created_at": created_at,
                "finalized_at": finalized_at,
                "author_deadline": author_deadline,
                "report": report,
                "is_fallback": is_fallback,
            }
        )

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.report_content import ReportContent

        d = dict(src_dict)
        id = d.pop("id")

        state = d.pop("state")

        def _parse_reason(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        reason = _parse_reason(d.pop("reason"))

        local_date = d.pop("local_date")

        timezone = d.pop("timezone")

        planned_at = d.pop("planned_at")

        window_start = d.pop("window_start")

        window_end = d.pop("window_end")

        def _parse_brief_hash(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        brief_hash = _parse_brief_hash(d.pop("brief_hash"))

        created_at = d.pop("created_at")

        def _parse_finalized_at(data: object) -> float | None:
            if data is None:
                return data
            return cast(float | None, data)

        finalized_at = _parse_finalized_at(d.pop("finalized_at"))

        author_deadline = d.pop("author_deadline")

        def _parse_report(data: object) -> None | ReportContent:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                report_type_0 = ReportContent.from_dict(data)

                return report_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ReportContent, data)

        report = _parse_report(d.pop("report"))

        is_fallback = d.pop("is_fallback")

        morning_report_record = cls(
            id=id,
            state=state,
            reason=reason,
            local_date=local_date,
            timezone=timezone,
            planned_at=planned_at,
            window_start=window_start,
            window_end=window_end,
            brief_hash=brief_hash,
            created_at=created_at,
            finalized_at=finalized_at,
            author_deadline=author_deadline,
            report=report,
            is_fallback=is_fallback,
        )

        morning_report_record.additional_properties = d
        return morning_report_record

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
