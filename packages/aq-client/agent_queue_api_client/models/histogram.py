from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="Histogram")


@_attrs_define
class Histogram:
    """Fixed millisecond buckets from src.metrics.histogram, merged by addition.

    Attributes:
        kind (str | Unset):  Default: 'hist'.
        counts (list[float] | Unset):
        count (float | Unset):  Default: 0.0.
        sum_ (float | Unset):  Default: 0.0.
        max_ (float | Unset):  Default: 0.0.
    """

    kind: str | Unset = "hist"
    counts: list[float] | Unset = UNSET
    count: float | Unset = 0.0
    sum_: float | Unset = 0.0
    max_: float | Unset = 0.0
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        kind = self.kind

        counts: list[float] | Unset = UNSET
        if not isinstance(self.counts, Unset):
            counts = self.counts

        count = self.count

        sum_ = self.sum_

        max_ = self.max_

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if kind is not UNSET:
            field_dict["kind"] = kind
        if counts is not UNSET:
            field_dict["counts"] = counts
        if count is not UNSET:
            field_dict["count"] = count
        if sum_ is not UNSET:
            field_dict["sum"] = sum_
        if max_ is not UNSET:
            field_dict["max"] = max_

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = d.pop("kind", UNSET)

        counts = cast(list[float], d.pop("counts", UNSET))

        count = d.pop("count", UNSET)

        sum_ = d.pop("sum", UNSET)

        max_ = d.pop("max", UNSET)

        histogram = cls(
            kind=kind,
            counts=counts,
            count=count,
            sum_=sum_,
            max_=max_,
        )

        histogram.additional_properties = d
        return histogram

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
