from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="DeliveryLatencyTarget")


@_attrs_define
class DeliveryLatencyTarget:
    """Section 9.2's per-project threshold, which is also the SLO target.

    ``source`` is ``floor`` until integration CI has been measured.

        Attributes:
            project_id (None | str | Unset):
            target_seconds (float | None | Unset):
            min_seconds (float | None | Unset):
            ci_multiplier (float | None | Unset):
            p90_batch_ci_seconds (float | None | Unset):
            batch_ci_samples (int | Unset):  Default: 0.
            source (None | str | Unset):
    """

    project_id: None | str | Unset = UNSET
    target_seconds: float | None | Unset = UNSET
    min_seconds: float | None | Unset = UNSET
    ci_multiplier: float | None | Unset = UNSET
    p90_batch_ci_seconds: float | None | Unset = UNSET
    batch_ci_samples: int | Unset = 0
    source: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        target_seconds: float | None | Unset
        if isinstance(self.target_seconds, Unset):
            target_seconds = UNSET
        else:
            target_seconds = self.target_seconds

        min_seconds: float | None | Unset
        if isinstance(self.min_seconds, Unset):
            min_seconds = UNSET
        else:
            min_seconds = self.min_seconds

        ci_multiplier: float | None | Unset
        if isinstance(self.ci_multiplier, Unset):
            ci_multiplier = UNSET
        else:
            ci_multiplier = self.ci_multiplier

        p90_batch_ci_seconds: float | None | Unset
        if isinstance(self.p90_batch_ci_seconds, Unset):
            p90_batch_ci_seconds = UNSET
        else:
            p90_batch_ci_seconds = self.p90_batch_ci_seconds

        batch_ci_samples = self.batch_ci_samples

        source: None | str | Unset
        if isinstance(self.source, Unset):
            source = UNSET
        else:
            source = self.source

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if target_seconds is not UNSET:
            field_dict["target_seconds"] = target_seconds
        if min_seconds is not UNSET:
            field_dict["min_seconds"] = min_seconds
        if ci_multiplier is not UNSET:
            field_dict["ci_multiplier"] = ci_multiplier
        if p90_batch_ci_seconds is not UNSET:
            field_dict["p90_batch_ci_seconds"] = p90_batch_ci_seconds
        if batch_ci_samples is not UNSET:
            field_dict["batch_ci_samples"] = batch_ci_samples
        if source is not UNSET:
            field_dict["source"] = source

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        def _parse_target_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        target_seconds = _parse_target_seconds(d.pop("target_seconds", UNSET))

        def _parse_min_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        min_seconds = _parse_min_seconds(d.pop("min_seconds", UNSET))

        def _parse_ci_multiplier(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        ci_multiplier = _parse_ci_multiplier(d.pop("ci_multiplier", UNSET))

        def _parse_p90_batch_ci_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        p90_batch_ci_seconds = _parse_p90_batch_ci_seconds(d.pop("p90_batch_ci_seconds", UNSET))

        batch_ci_samples = d.pop("batch_ci_samples", UNSET)

        def _parse_source(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source = _parse_source(d.pop("source", UNSET))

        delivery_latency_target = cls(
            project_id=project_id,
            target_seconds=target_seconds,
            min_seconds=min_seconds,
            ci_multiplier=ci_multiplier,
            p90_batch_ci_seconds=p90_batch_ci_seconds,
            batch_ci_samples=batch_ci_samples,
            source=source,
        )

        delivery_latency_target.additional_properties = d
        return delivery_latency_target

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
