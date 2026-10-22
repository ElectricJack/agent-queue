from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_duration_metrics import WaitDurationMetrics


T = TypeVar("T", bound="WaitProjectMetrics")


@_attrs_define
class WaitProjectMetrics:
    """
    Attributes:
        resume_cost_p50 (float | None | Unset):
        resume_cost_p95 (float | None | Unset):
        resume_cost_minutes (float | None | Unset):
        summary_tokens (float | None | Unset):
        resume_tokens (float | None | Unset):
        teardown_seconds (float | None | Unset):
        setup_seconds (float | None | Unset):
        rebuild_seconds (float | None | Unset):
        durations (list[WaitDurationMetrics] | Unset):
    """

    resume_cost_p50: float | None | Unset = UNSET
    resume_cost_p95: float | None | Unset = UNSET
    resume_cost_minutes: float | None | Unset = UNSET
    summary_tokens: float | None | Unset = UNSET
    resume_tokens: float | None | Unset = UNSET
    teardown_seconds: float | None | Unset = UNSET
    setup_seconds: float | None | Unset = UNSET
    rebuild_seconds: float | None | Unset = UNSET
    durations: list[WaitDurationMetrics] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        resume_cost_p50: float | None | Unset
        if isinstance(self.resume_cost_p50, Unset):
            resume_cost_p50 = UNSET
        else:
            resume_cost_p50 = self.resume_cost_p50

        resume_cost_p95: float | None | Unset
        if isinstance(self.resume_cost_p95, Unset):
            resume_cost_p95 = UNSET
        else:
            resume_cost_p95 = self.resume_cost_p95

        resume_cost_minutes: float | None | Unset
        if isinstance(self.resume_cost_minutes, Unset):
            resume_cost_minutes = UNSET
        else:
            resume_cost_minutes = self.resume_cost_minutes

        summary_tokens: float | None | Unset
        if isinstance(self.summary_tokens, Unset):
            summary_tokens = UNSET
        else:
            summary_tokens = self.summary_tokens

        resume_tokens: float | None | Unset
        if isinstance(self.resume_tokens, Unset):
            resume_tokens = UNSET
        else:
            resume_tokens = self.resume_tokens

        teardown_seconds: float | None | Unset
        if isinstance(self.teardown_seconds, Unset):
            teardown_seconds = UNSET
        else:
            teardown_seconds = self.teardown_seconds

        setup_seconds: float | None | Unset
        if isinstance(self.setup_seconds, Unset):
            setup_seconds = UNSET
        else:
            setup_seconds = self.setup_seconds

        rebuild_seconds: float | None | Unset
        if isinstance(self.rebuild_seconds, Unset):
            rebuild_seconds = UNSET
        else:
            rebuild_seconds = self.rebuild_seconds

        durations: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.durations, Unset):
            durations = []
            for durations_item_data in self.durations:
                durations_item = durations_item_data.to_dict()
                durations.append(durations_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if resume_cost_p50 is not UNSET:
            field_dict["resume_cost_p50"] = resume_cost_p50
        if resume_cost_p95 is not UNSET:
            field_dict["resume_cost_p95"] = resume_cost_p95
        if resume_cost_minutes is not UNSET:
            field_dict["resume_cost_minutes"] = resume_cost_minutes
        if summary_tokens is not UNSET:
            field_dict["summary_tokens"] = summary_tokens
        if resume_tokens is not UNSET:
            field_dict["resume_tokens"] = resume_tokens
        if teardown_seconds is not UNSET:
            field_dict["teardown_seconds"] = teardown_seconds
        if setup_seconds is not UNSET:
            field_dict["setup_seconds"] = setup_seconds
        if rebuild_seconds is not UNSET:
            field_dict["rebuild_seconds"] = rebuild_seconds
        if durations is not UNSET:
            field_dict["durations"] = durations

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_duration_metrics import WaitDurationMetrics

        d = dict(src_dict)

        def _parse_resume_cost_p50(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        resume_cost_p50 = _parse_resume_cost_p50(d.pop("resume_cost_p50", UNSET))

        def _parse_resume_cost_p95(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        resume_cost_p95 = _parse_resume_cost_p95(d.pop("resume_cost_p95", UNSET))

        def _parse_resume_cost_minutes(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        resume_cost_minutes = _parse_resume_cost_minutes(d.pop("resume_cost_minutes", UNSET))

        def _parse_summary_tokens(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        summary_tokens = _parse_summary_tokens(d.pop("summary_tokens", UNSET))

        def _parse_resume_tokens(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        resume_tokens = _parse_resume_tokens(d.pop("resume_tokens", UNSET))

        def _parse_teardown_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        teardown_seconds = _parse_teardown_seconds(d.pop("teardown_seconds", UNSET))

        def _parse_setup_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        setup_seconds = _parse_setup_seconds(d.pop("setup_seconds", UNSET))

        def _parse_rebuild_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        rebuild_seconds = _parse_rebuild_seconds(d.pop("rebuild_seconds", UNSET))

        _durations = d.pop("durations", UNSET)
        durations: list[WaitDurationMetrics] | Unset = UNSET
        if _durations is not UNSET:
            durations = []
            for durations_item_data in _durations:
                durations_item = WaitDurationMetrics.from_dict(durations_item_data)

                durations.append(durations_item)

        wait_project_metrics = cls(
            resume_cost_p50=resume_cost_p50,
            resume_cost_p95=resume_cost_p95,
            resume_cost_minutes=resume_cost_minutes,
            summary_tokens=summary_tokens,
            resume_tokens=resume_tokens,
            teardown_seconds=teardown_seconds,
            setup_seconds=setup_seconds,
            rebuild_seconds=rebuild_seconds,
            durations=durations,
        )

        wait_project_metrics.additional_properties = d
        return wait_project_metrics

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
