from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

T = TypeVar("T", bound="BenchmarkStageRecordResponse")


@_attrs_define
class BenchmarkStageRecordResponse:
    """
    Attributes:
        span_id (str):
        inserted (bool):
        duration_ms (float):
        stage (str):
        success (bool | Unset):  Default: True.
        session_attempt_id (None | str | Unset):
    """

    span_id: str
    inserted: bool
    duration_ms: float
    stage: str
    success: bool | Unset = True
    session_attempt_id: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        span_id = self.span_id

        inserted = self.inserted

        duration_ms = self.duration_ms

        stage = self.stage

        success = self.success

        session_attempt_id: None | str | Unset
        if isinstance(self.session_attempt_id, Unset):
            session_attempt_id = UNSET
        else:
            session_attempt_id = self.session_attempt_id

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "span_id": span_id,
                "inserted": inserted,
                "duration_ms": duration_ms,
                "stage": stage,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if session_attempt_id is not UNSET:
            field_dict["session_attempt_id"] = session_attempt_id

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        span_id = d.pop("span_id")

        inserted = d.pop("inserted")

        duration_ms = d.pop("duration_ms")

        stage = d.pop("stage")

        success = d.pop("success", UNSET)

        def _parse_session_attempt_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        session_attempt_id = _parse_session_attempt_id(d.pop("session_attempt_id", UNSET))

        benchmark_stage_record_response = cls(
            span_id=span_id,
            inserted=inserted,
            duration_ms=duration_ms,
            stage=stage,
            success=success,
            session_attempt_id=session_attempt_id,
        )

        benchmark_stage_record_response.additional_properties = d
        return benchmark_stage_record_response

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
