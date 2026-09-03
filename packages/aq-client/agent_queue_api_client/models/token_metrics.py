from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.token_metrics_by_model import TokenMetricsByModel


T = TypeVar("T", bound="TokenMetrics")


@_attrs_define
class TokenMetrics:
    """Ledger rates over ``window_seconds``, scaled to per minute.

    ``total_per_min`` is everything the ledger recorded, cache included: on a
    long-lived session cache reads are the overwhelming majority of the
    traffic, so a "total" of input+output alone understates it by orders of
    magnitude.  ``unattributed_per_min`` is what no column could account for
    — rows from writers that report only a total, or written before the cache
    columns existed — reported separately rather than folded into a model's
    rate, the same honesty rule ``get_costs`` applies to pricing.

    The ``*_per_min_1m`` fields are the raw trailing-minute counts, kept
    beside the smoothed rates so the unsmoothed flush pattern is still
    readable.

        Attributes:
            input_per_min (float | Unset):  Default: 0.0.
            output_per_min (float | Unset):  Default: 0.0.
            cache_read_per_min (float | Unset):  Default: 0.0.
            cache_write_per_min (float | Unset):  Default: 0.0.
            total_per_min (float | Unset):  Default: 0.0.
            unattributed_per_min (float | Unset):  Default: 0.0.
            input_per_min_1m (float | Unset):  Default: 0.0.
            output_per_min_1m (float | Unset):  Default: 0.0.
            total_per_min_1m (float | Unset):  Default: 0.0.
            window_seconds (float | Unset):  Default: 60.0.
            by_model (TokenMetricsByModel | Unset):
    """

    input_per_min: float | Unset = 0.0
    output_per_min: float | Unset = 0.0
    cache_read_per_min: float | Unset = 0.0
    cache_write_per_min: float | Unset = 0.0
    total_per_min: float | Unset = 0.0
    unattributed_per_min: float | Unset = 0.0
    input_per_min_1m: float | Unset = 0.0
    output_per_min_1m: float | Unset = 0.0
    total_per_min_1m: float | Unset = 0.0
    window_seconds: float | Unset = 60.0
    by_model: TokenMetricsByModel | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        input_per_min = self.input_per_min

        output_per_min = self.output_per_min

        cache_read_per_min = self.cache_read_per_min

        cache_write_per_min = self.cache_write_per_min

        total_per_min = self.total_per_min

        unattributed_per_min = self.unattributed_per_min

        input_per_min_1m = self.input_per_min_1m

        output_per_min_1m = self.output_per_min_1m

        total_per_min_1m = self.total_per_min_1m

        window_seconds = self.window_seconds

        by_model: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_model, Unset):
            by_model = self.by_model.to_dict()

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if input_per_min is not UNSET:
            field_dict["input_per_min"] = input_per_min
        if output_per_min is not UNSET:
            field_dict["output_per_min"] = output_per_min
        if cache_read_per_min is not UNSET:
            field_dict["cache_read_per_min"] = cache_read_per_min
        if cache_write_per_min is not UNSET:
            field_dict["cache_write_per_min"] = cache_write_per_min
        if total_per_min is not UNSET:
            field_dict["total_per_min"] = total_per_min
        if unattributed_per_min is not UNSET:
            field_dict["unattributed_per_min"] = unattributed_per_min
        if input_per_min_1m is not UNSET:
            field_dict["input_per_min_1m"] = input_per_min_1m
        if output_per_min_1m is not UNSET:
            field_dict["output_per_min_1m"] = output_per_min_1m
        if total_per_min_1m is not UNSET:
            field_dict["total_per_min_1m"] = total_per_min_1m
        if window_seconds is not UNSET:
            field_dict["window_seconds"] = window_seconds
        if by_model is not UNSET:
            field_dict["by_model"] = by_model

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.token_metrics_by_model import TokenMetricsByModel

        d = dict(src_dict)
        input_per_min = d.pop("input_per_min", UNSET)

        output_per_min = d.pop("output_per_min", UNSET)

        cache_read_per_min = d.pop("cache_read_per_min", UNSET)

        cache_write_per_min = d.pop("cache_write_per_min", UNSET)

        total_per_min = d.pop("total_per_min", UNSET)

        unattributed_per_min = d.pop("unattributed_per_min", UNSET)

        input_per_min_1m = d.pop("input_per_min_1m", UNSET)

        output_per_min_1m = d.pop("output_per_min_1m", UNSET)

        total_per_min_1m = d.pop("total_per_min_1m", UNSET)

        window_seconds = d.pop("window_seconds", UNSET)

        _by_model = d.pop("by_model", UNSET)
        by_model: TokenMetricsByModel | Unset
        if isinstance(_by_model, Unset):
            by_model = UNSET
        else:
            by_model = TokenMetricsByModel.from_dict(_by_model)

        token_metrics = cls(
            input_per_min=input_per_min,
            output_per_min=output_per_min,
            cache_read_per_min=cache_read_per_min,
            cache_write_per_min=cache_write_per_min,
            total_per_min=total_per_min,
            unattributed_per_min=unattributed_per_min,
            input_per_min_1m=input_per_min_1m,
            output_per_min_1m=output_per_min_1m,
            total_per_min_1m=total_per_min_1m,
            window_seconds=window_seconds,
            by_model=by_model,
        )

        token_metrics.additional_properties = d
        return token_metrics

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
