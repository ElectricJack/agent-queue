from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.assignment_route_detail import AssignmentRouteDetail
    from ..models.claim_timing_detail import ClaimTimingDetail
    from ..models.explain_reason import ExplainReason
    from ..models.explain_task_entry import ExplainTaskEntry
    from ..models.phase_hold_detail import PhaseHoldDetail
    from ..models.provider_hold_detail import ProviderHoldDetail


T = TypeVar("T", bound="ExplainTaskResponse")


@_attrs_define
class ExplainTaskResponse:
    """
    Attributes:
        success (bool | Unset):  Default: True.
        reasons (list[ExplainReason] | Unset):
        reason_codes (list[str] | Unset):
        assignment_route (AssignmentRouteDetail | None | Unset):
        provider_hold (None | ProviderHoldDetail | Unset):
        phase_hold (None | PhaseHoldDetail | Unset):
        claim_timing (ClaimTimingDetail | None | Unset):
        explanations (list[ExplainTaskEntry] | None | Unset):
    """

    success: bool | Unset = True
    reasons: list[ExplainReason] | Unset = UNSET
    reason_codes: list[str] | Unset = UNSET
    assignment_route: AssignmentRouteDetail | None | Unset = UNSET
    provider_hold: None | ProviderHoldDetail | Unset = UNSET
    phase_hold: None | PhaseHoldDetail | Unset = UNSET
    claim_timing: ClaimTimingDetail | None | Unset = UNSET
    explanations: list[ExplainTaskEntry] | None | Unset = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.assignment_route_detail import AssignmentRouteDetail
        from ..models.claim_timing_detail import ClaimTimingDetail
        from ..models.phase_hold_detail import PhaseHoldDetail
        from ..models.provider_hold_detail import ProviderHoldDetail

        success = self.success

        reasons: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.reasons, Unset):
            reasons = []
            for reasons_item_data in self.reasons:
                reasons_item = reasons_item_data.to_dict()
                reasons.append(reasons_item)

        reason_codes: list[str] | Unset = UNSET
        if not isinstance(self.reason_codes, Unset):
            reason_codes = self.reason_codes

        assignment_route: dict[str, Any] | None | Unset
        if isinstance(self.assignment_route, Unset):
            assignment_route = UNSET
        elif isinstance(self.assignment_route, AssignmentRouteDetail):
            assignment_route = self.assignment_route.to_dict()
        else:
            assignment_route = self.assignment_route

        provider_hold: dict[str, Any] | None | Unset
        if isinstance(self.provider_hold, Unset):
            provider_hold = UNSET
        elif isinstance(self.provider_hold, ProviderHoldDetail):
            provider_hold = self.provider_hold.to_dict()
        else:
            provider_hold = self.provider_hold

        phase_hold: dict[str, Any] | None | Unset
        if isinstance(self.phase_hold, Unset):
            phase_hold = UNSET
        elif isinstance(self.phase_hold, PhaseHoldDetail):
            phase_hold = self.phase_hold.to_dict()
        else:
            phase_hold = self.phase_hold

        claim_timing: dict[str, Any] | None | Unset
        if isinstance(self.claim_timing, Unset):
            claim_timing = UNSET
        elif isinstance(self.claim_timing, ClaimTimingDetail):
            claim_timing = self.claim_timing.to_dict()
        else:
            claim_timing = self.claim_timing

        explanations: list[dict[str, Any]] | None | Unset
        if isinstance(self.explanations, Unset):
            explanations = UNSET
        elif isinstance(self.explanations, list):
            explanations = []
            for explanations_type_0_item_data in self.explanations:
                explanations_type_0_item = explanations_type_0_item_data.to_dict()
                explanations.append(explanations_type_0_item)

        else:
            explanations = self.explanations

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if success is not UNSET:
            field_dict["success"] = success
        if reasons is not UNSET:
            field_dict["reasons"] = reasons
        if reason_codes is not UNSET:
            field_dict["reason_codes"] = reason_codes
        if assignment_route is not UNSET:
            field_dict["assignment_route"] = assignment_route
        if provider_hold is not UNSET:
            field_dict["provider_hold"] = provider_hold
        if phase_hold is not UNSET:
            field_dict["phase_hold"] = phase_hold
        if claim_timing is not UNSET:
            field_dict["claim_timing"] = claim_timing
        if explanations is not UNSET:
            field_dict["explanations"] = explanations

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.assignment_route_detail import AssignmentRouteDetail
        from ..models.claim_timing_detail import ClaimTimingDetail
        from ..models.explain_reason import ExplainReason
        from ..models.explain_task_entry import ExplainTaskEntry
        from ..models.phase_hold_detail import PhaseHoldDetail
        from ..models.provider_hold_detail import ProviderHoldDetail

        d = dict(src_dict)
        success = d.pop("success", UNSET)

        _reasons = d.pop("reasons", UNSET)
        reasons: list[ExplainReason] | Unset = UNSET
        if _reasons is not UNSET:
            reasons = []
            for reasons_item_data in _reasons:
                reasons_item = ExplainReason.from_dict(reasons_item_data)

                reasons.append(reasons_item)

        reason_codes = cast(list[str], d.pop("reason_codes", UNSET))

        def _parse_assignment_route(data: object) -> AssignmentRouteDetail | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                assignment_route_type_0 = AssignmentRouteDetail.from_dict(data)

                return assignment_route_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AssignmentRouteDetail | None | Unset, data)

        assignment_route = _parse_assignment_route(d.pop("assignment_route", UNSET))

        def _parse_provider_hold(data: object) -> None | ProviderHoldDetail | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                provider_hold_type_0 = ProviderHoldDetail.from_dict(data)

                return provider_hold_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | ProviderHoldDetail | Unset, data)

        provider_hold = _parse_provider_hold(d.pop("provider_hold", UNSET))

        def _parse_phase_hold(data: object) -> None | PhaseHoldDetail | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                phase_hold_type_0 = PhaseHoldDetail.from_dict(data)

                return phase_hold_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PhaseHoldDetail | Unset, data)

        phase_hold = _parse_phase_hold(d.pop("phase_hold", UNSET))

        def _parse_claim_timing(data: object) -> ClaimTimingDetail | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                claim_timing_type_0 = ClaimTimingDetail.from_dict(data)

                return claim_timing_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ClaimTimingDetail | None | Unset, data)

        claim_timing = _parse_claim_timing(d.pop("claim_timing", UNSET))

        def _parse_explanations(data: object) -> list[ExplainTaskEntry] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                explanations_type_0 = []
                _explanations_type_0 = data
                for explanations_type_0_item_data in _explanations_type_0:
                    explanations_type_0_item = ExplainTaskEntry.from_dict(explanations_type_0_item_data)

                    explanations_type_0.append(explanations_type_0_item)

                return explanations_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[ExplainTaskEntry] | None | Unset, data)

        explanations = _parse_explanations(d.pop("explanations", UNSET))

        explain_task_response = cls(
            success=success,
            reasons=reasons,
            reason_codes=reason_codes,
            assignment_route=assignment_route,
            provider_hold=provider_hold,
            phase_hold=phase_hold,
            claim_timing=claim_timing,
            explanations=explanations,
        )

        explain_task_response.additional_properties = d
        return explain_task_response

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
