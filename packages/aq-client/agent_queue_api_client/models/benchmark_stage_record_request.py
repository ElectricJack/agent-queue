from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="BenchmarkStageRecordRequest")


@_attrs_define
class BenchmarkStageRecordRequest:
    """
    Attributes:
        task_id (str):
        span_id (str):
        stage (str):
        started_monotonic_ns (int):
        ended_monotonic_ns (int):
        claim_epoch (int | None | Unset):
    """

    task_id: str
    span_id: str
    stage: str
    started_monotonic_ns: int
    ended_monotonic_ns: int
    claim_epoch: int | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        task_id = self.task_id

        span_id = self.span_id

        stage = self.stage

        started_monotonic_ns = self.started_monotonic_ns

        ended_monotonic_ns = self.ended_monotonic_ns

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "task_id": task_id,
                "span_id": span_id,
                "stage": stage,
                "started_monotonic_ns": started_monotonic_ns,
                "ended_monotonic_ns": ended_monotonic_ns,
            }
        )
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        task_id = d.pop("task_id")

        span_id = d.pop("span_id")

        stage = d.pop("stage")

        started_monotonic_ns = d.pop("started_monotonic_ns")

        ended_monotonic_ns = d.pop("ended_monotonic_ns")

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        benchmark_stage_record_request = cls(
            task_id=task_id,
            span_id=span_id,
            stage=stage,
            started_monotonic_ns=started_monotonic_ns,
            ended_monotonic_ns=ended_monotonic_ns,
            claim_epoch=claim_epoch,
        )

        benchmark_stage_record_request.additional_properties = d
        return benchmark_stage_record_request

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
