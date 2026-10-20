from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="WaitDurationMetrics")


@_attrs_define
class WaitDurationMetrics:
    """
    Attributes:
        condition_kind (str):
        source_class (str):
        wait_duration_p50 (float | None | Unset):
        expected_duration_seconds (float | None | Unset):
    """

    condition_kind: str
    source_class: str
    wait_duration_p50: float | None | Unset = UNSET
    expected_duration_seconds: float | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        condition_kind = self.condition_kind

        source_class = self.source_class

        wait_duration_p50: float | None | Unset
        if isinstance(self.wait_duration_p50, Unset):
            wait_duration_p50 = UNSET
        else:
            wait_duration_p50 = self.wait_duration_p50

        expected_duration_seconds: float | None | Unset
        if isinstance(self.expected_duration_seconds, Unset):
            expected_duration_seconds = UNSET
        else:
            expected_duration_seconds = self.expected_duration_seconds

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "condition_kind": condition_kind,
                "source_class": source_class,
            }
        )
        if wait_duration_p50 is not UNSET:
            field_dict["wait_duration_p50"] = wait_duration_p50
        if expected_duration_seconds is not UNSET:
            field_dict["expected_duration_seconds"] = expected_duration_seconds

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        condition_kind = d.pop("condition_kind")

        source_class = d.pop("source_class")

        def _parse_wait_duration_p50(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        wait_duration_p50 = _parse_wait_duration_p50(d.pop("wait_duration_p50", UNSET))

        def _parse_expected_duration_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        expected_duration_seconds = _parse_expected_duration_seconds(d.pop("expected_duration_seconds", UNSET))

        wait_duration_metrics = cls(
            condition_kind=condition_kind,
            source_class=source_class,
            wait_duration_p50=wait_duration_p50,
            expected_duration_seconds=expected_duration_seconds,
        )

        wait_duration_metrics.additional_properties = d
        return wait_duration_metrics

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
