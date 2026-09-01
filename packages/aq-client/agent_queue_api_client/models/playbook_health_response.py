from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_health_response_metrics import PlaybookHealthResponseMetrics


T = TypeVar("T", bound="PlaybookHealthResponse")


@_attrs_define
class PlaybookHealthResponse:
    """Loose shape — compute_playbook_health returns a rich dynamic dict.

    Attributes:
        playbook_id (None | str | Unset):
        run_count (int | Unset):  Default: 0.
        success_rate (float | Unset):  Default: 0.0.
        avg_tokens (float | Unset):  Default: 0.0.
        avg_duration_seconds (float | Unset):  Default: 0.0.
        metrics (PlaybookHealthResponseMetrics | Unset):
    """

    playbook_id: None | str | Unset = UNSET
    run_count: int | Unset = 0
    success_rate: float | Unset = 0.0
    avg_tokens: float | Unset = 0.0
    avg_duration_seconds: float | Unset = 0.0
    metrics: PlaybookHealthResponseMetrics | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        playbook_id: None | str | Unset
        if isinstance(self.playbook_id, Unset):
            playbook_id = UNSET
        else:
            playbook_id = self.playbook_id

        run_count = self.run_count

        success_rate = self.success_rate

        avg_tokens = self.avg_tokens

        avg_duration_seconds = self.avg_duration_seconds

        metrics: dict[str, Any] | Unset = UNSET
        if not isinstance(self.metrics, Unset):
            metrics = self.metrics.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if playbook_id is not UNSET:
            field_dict["playbook_id"] = playbook_id
        if run_count is not UNSET:
            field_dict["run_count"] = run_count
        if success_rate is not UNSET:
            field_dict["success_rate"] = success_rate
        if avg_tokens is not UNSET:
            field_dict["avg_tokens"] = avg_tokens
        if avg_duration_seconds is not UNSET:
            field_dict["avg_duration_seconds"] = avg_duration_seconds
        if metrics is not UNSET:
            field_dict["metrics"] = metrics

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_health_response_metrics import PlaybookHealthResponseMetrics

        d = dict(src_dict)

        def _parse_playbook_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        playbook_id = _parse_playbook_id(d.pop("playbook_id", UNSET))

        run_count = d.pop("run_count", UNSET)

        success_rate = d.pop("success_rate", UNSET)

        avg_tokens = d.pop("avg_tokens", UNSET)

        avg_duration_seconds = d.pop("avg_duration_seconds", UNSET)

        _metrics = d.pop("metrics", UNSET)
        metrics: PlaybookHealthResponseMetrics | Unset
        if isinstance(_metrics, Unset):
            metrics = UNSET
        else:
            metrics = PlaybookHealthResponseMetrics.from_dict(_metrics)

        playbook_health_response = cls(
            playbook_id=playbook_id,
            run_count=run_count,
            success_rate=success_rate,
            avg_tokens=avg_tokens,
            avg_duration_seconds=avg_duration_seconds,
            metrics=metrics,
        )

        playbook_health_response.additional_properties = d
        return playbook_health_response

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
