from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="MorningReportPreviewRequest")


@_attrs_define
class MorningReportPreviewRequest:
    """
    Attributes:
        now (float | None | Unset):
        since (float | None | Unset):
        until (float | None | Unset):
        project_ids (list[Any] | None | Unset):
        max_lookback_hours (int | None | Unset):
    """

    now: float | None | Unset = UNSET
    since: float | None | Unset = UNSET
    until: float | None | Unset = UNSET
    project_ids: list[Any] | None | Unset = UNSET
    max_lookback_hours: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        now: float | None | Unset
        if isinstance(self.now, Unset):
            now = UNSET
        else:
            now = self.now

        since: float | None | Unset
        if isinstance(self.since, Unset):
            since = UNSET
        else:
            since = self.since

        until: float | None | Unset
        if isinstance(self.until, Unset):
            until = UNSET
        else:
            until = self.until

        project_ids: list[Any] | None | Unset
        if isinstance(self.project_ids, Unset):
            project_ids = UNSET
        elif isinstance(self.project_ids, list):
            project_ids = self.project_ids

        else:
            project_ids = self.project_ids

        max_lookback_hours: int | None | Unset
        if isinstance(self.max_lookback_hours, Unset):
            max_lookback_hours = UNSET
        else:
            max_lookback_hours = self.max_lookback_hours

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if now is not UNSET:
            field_dict["now"] = now
        if since is not UNSET:
            field_dict["since"] = since
        if until is not UNSET:
            field_dict["until"] = until
        if project_ids is not UNSET:
            field_dict["project_ids"] = project_ids
        if max_lookback_hours is not UNSET:
            field_dict["max_lookback_hours"] = max_lookback_hours

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_now(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        now = _parse_now(d.pop("now", UNSET))

        def _parse_since(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        since = _parse_since(d.pop("since", UNSET))

        def _parse_until(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        until = _parse_until(d.pop("until", UNSET))

        def _parse_project_ids(data: object) -> list[Any] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                project_ids_type_0 = cast(list[Any], data)

                return project_ids_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[Any] | None | Unset, data)

        project_ids = _parse_project_ids(d.pop("project_ids", UNSET))

        def _parse_max_lookback_hours(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        max_lookback_hours = _parse_max_lookback_hours(d.pop("max_lookback_hours", UNSET))

        morning_report_preview_request = cls(
            now=now,
            since=since,
            until=until,
            project_ids=project_ids,
            max_lookback_hours=max_lookback_hours,
        )

        morning_report_preview_request.additional_properties = d
        return morning_report_preview_request

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
