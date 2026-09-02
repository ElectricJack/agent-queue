from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.metrics_sample import MetricsSample


T = TypeVar("T", bound="MetricsSeriesResponse")


@_attrs_define
class MetricsSeriesResponse:
    """``GET /api/metrics/series``.

    ``step`` is the resolution actually served, which may be coarser than
    the one requested when ``step=auto`` or when the requested span would
    exceed ``max_points``.

        Attributes:
            step (str):
            from_ts (float):
            to_ts (float):
            truncated (bool | Unset):  Default: False.
            samples (list[MetricsSample] | Unset):
    """

    step: str
    from_ts: float
    to_ts: float
    truncated: bool | Unset = False
    samples: list[MetricsSample] | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        step = self.step

        from_ts = self.from_ts

        to_ts = self.to_ts

        truncated = self.truncated

        samples: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.samples, Unset):
            samples = []
            for samples_item_data in self.samples:
                samples_item = samples_item_data.to_dict()
                samples.append(samples_item)

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "step": step,
                "from_ts": from_ts,
                "to_ts": to_ts,
            }
        )
        if truncated is not UNSET:
            field_dict["truncated"] = truncated
        if samples is not UNSET:
            field_dict["samples"] = samples

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.metrics_sample import MetricsSample

        d = dict(src_dict)
        step = d.pop("step")

        from_ts = d.pop("from_ts")

        to_ts = d.pop("to_ts")

        truncated = d.pop("truncated", UNSET)

        _samples = d.pop("samples", UNSET)
        samples: list[MetricsSample] | Unset = UNSET
        if _samples is not UNSET:
            samples = []
            for samples_item_data in _samples:
                samples_item = MetricsSample.from_dict(samples_item_data)

                samples.append(samples_item)

        metrics_series_response = cls(
            step=step,
            from_ts=from_ts,
            to_ts=to_ts,
            truncated=truncated,
            samples=samples,
        )

        metrics_series_response.additional_properties = d
        return metrics_series_response

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
