from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.claim_timing_detail_phases import ClaimTimingDetailPhases


T = TypeVar("T", bound="ClaimTimingDetail")


@_attrs_define
class ClaimTimingDetail:
    """
    Attributes:
        session_id (str):
        total_seconds (float):
        claim_epoch (int | None | Unset):
        phases (ClaimTimingDetailPhases | Unset):
        current_phase (None | str | Unset):
        result (None | str | Unset):
    """

    session_id: str
    total_seconds: float
    claim_epoch: int | None | Unset = UNSET
    phases: ClaimTimingDetailPhases | Unset = UNSET
    current_phase: None | str | Unset = UNSET
    result: None | str | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        session_id = self.session_id

        total_seconds = self.total_seconds

        claim_epoch: int | None | Unset
        if isinstance(self.claim_epoch, Unset):
            claim_epoch = UNSET
        else:
            claim_epoch = self.claim_epoch

        phases: dict[str, Any] | Unset = UNSET
        if not isinstance(self.phases, Unset):
            phases = self.phases.to_dict()

        current_phase: None | str | Unset
        if isinstance(self.current_phase, Unset):
            current_phase = UNSET
        else:
            current_phase = self.current_phase

        result: None | str | Unset
        if isinstance(self.result, Unset):
            result = UNSET
        else:
            result = self.result

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update(
            {
                "session_id": session_id,
                "total_seconds": total_seconds,
            }
        )
        if claim_epoch is not UNSET:
            field_dict["claim_epoch"] = claim_epoch
        if phases is not UNSET:
            field_dict["phases"] = phases
        if current_phase is not UNSET:
            field_dict["current_phase"] = current_phase
        if result is not UNSET:
            field_dict["result"] = result

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.claim_timing_detail_phases import ClaimTimingDetailPhases

        d = dict(src_dict)
        session_id = d.pop("session_id")

        total_seconds = d.pop("total_seconds")

        def _parse_claim_epoch(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        claim_epoch = _parse_claim_epoch(d.pop("claim_epoch", UNSET))

        _phases = d.pop("phases", UNSET)
        phases: ClaimTimingDetailPhases | Unset
        if isinstance(_phases, Unset):
            phases = UNSET
        else:
            phases = ClaimTimingDetailPhases.from_dict(_phases)

        def _parse_current_phase(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_phase = _parse_current_phase(d.pop("current_phase", UNSET))

        def _parse_result(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result = _parse_result(d.pop("result", UNSET))

        claim_timing_detail = cls(
            session_id=session_id,
            total_seconds=total_seconds,
            claim_epoch=claim_epoch,
            phases=phases,
            current_phase=current_phase,
            result=result,
        )

        claim_timing_detail.additional_properties = d
        return claim_timing_detail

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
