from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_counterfactual import WaitCounterfactual
    from ..models.wait_metrics_by_project import WaitMetricsByProject
    from ..models.wait_metrics_scheduler_by_reason import WaitMetricsSchedulerByReason
    from ..models.wait_priority_metrics import WaitPriorityMetrics
    from ..models.wait_slot_seconds import WaitSlotSeconds
    from ..models.wait_wakes import WaitWakes


T = TypeVar("T", bound="WaitMetrics")


@_attrs_define
class WaitMetrics:
    """Observed wait accounting. Missing observations remain null at every tier.

    Resume cost percentiles are seconds; configured resume_cost_minutes stays
    in minutes. Duration/cost cohorts cover the preceding thirty days.

        Attributes:
            pending_in_place (float | None | Unset):
            pending_hibernated (float | None | Unset):
            pending_human (float | None | Unset):
            escalated (float | None | Unset):
            scheduler_by_reason (WaitMetricsSchedulerByReason | Unset):
            in_place_slot_seconds_1h (float | None | Unset):
            released_slot_seconds_1h (None | Unset | WaitSlotSeconds):
            released_slot_seconds_24h (None | Unset | WaitSlotSeconds):
            evicted_1h (float | None | Unset):
            ceiling_1h (float | None | Unset):
            resume_cost_p50 (float | None | Unset):
            resume_cost_p95 (float | None | Unset):
            by_project (WaitMetricsByProject | Unset):
            wakes_1h (None | Unset | WaitWakes):
            loops_1h (float | None | Unset):
            escalations_1h (float | None | Unset):
            decisions_1h (float | None | Unset):
            decision_seconds_p50 (float | None | Unset):
            human_pending (float | None | Unset):
            human_age_max_seconds (float | None | Unset):
            priority (None | Unset | WaitPriorityMetrics):
            wait_seconds_1h (float | None | Unset):
            overhead_tokens_1h (float | None | Unset):
            tokens_saved_counterfactual (None | Unset | WaitCounterfactual):
    """

    pending_in_place: float | None | Unset = UNSET
    pending_hibernated: float | None | Unset = UNSET
    pending_human: float | None | Unset = UNSET
    escalated: float | None | Unset = UNSET
    scheduler_by_reason: WaitMetricsSchedulerByReason | Unset = UNSET
    in_place_slot_seconds_1h: float | None | Unset = UNSET
    released_slot_seconds_1h: None | Unset | WaitSlotSeconds = UNSET
    released_slot_seconds_24h: None | Unset | WaitSlotSeconds = UNSET
    evicted_1h: float | None | Unset = UNSET
    ceiling_1h: float | None | Unset = UNSET
    resume_cost_p50: float | None | Unset = UNSET
    resume_cost_p95: float | None | Unset = UNSET
    by_project: WaitMetricsByProject | Unset = UNSET
    wakes_1h: None | Unset | WaitWakes = UNSET
    loops_1h: float | None | Unset = UNSET
    escalations_1h: float | None | Unset = UNSET
    decisions_1h: float | None | Unset = UNSET
    decision_seconds_p50: float | None | Unset = UNSET
    human_pending: float | None | Unset = UNSET
    human_age_max_seconds: float | None | Unset = UNSET
    priority: None | Unset | WaitPriorityMetrics = UNSET
    wait_seconds_1h: float | None | Unset = UNSET
    overhead_tokens_1h: float | None | Unset = UNSET
    tokens_saved_counterfactual: None | Unset | WaitCounterfactual = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.wait_counterfactual import WaitCounterfactual
        from ..models.wait_priority_metrics import WaitPriorityMetrics
        from ..models.wait_slot_seconds import WaitSlotSeconds
        from ..models.wait_wakes import WaitWakes

        pending_in_place: float | None | Unset
        if isinstance(self.pending_in_place, Unset):
            pending_in_place = UNSET
        else:
            pending_in_place = self.pending_in_place

        pending_hibernated: float | None | Unset
        if isinstance(self.pending_hibernated, Unset):
            pending_hibernated = UNSET
        else:
            pending_hibernated = self.pending_hibernated

        pending_human: float | None | Unset
        if isinstance(self.pending_human, Unset):
            pending_human = UNSET
        else:
            pending_human = self.pending_human

        escalated: float | None | Unset
        if isinstance(self.escalated, Unset):
            escalated = UNSET
        else:
            escalated = self.escalated

        scheduler_by_reason: dict[str, Any] | Unset = UNSET
        if not isinstance(self.scheduler_by_reason, Unset):
            scheduler_by_reason = self.scheduler_by_reason.to_dict()

        in_place_slot_seconds_1h: float | None | Unset
        if isinstance(self.in_place_slot_seconds_1h, Unset):
            in_place_slot_seconds_1h = UNSET
        else:
            in_place_slot_seconds_1h = self.in_place_slot_seconds_1h

        released_slot_seconds_1h: dict[str, Any] | None | Unset
        if isinstance(self.released_slot_seconds_1h, Unset):
            released_slot_seconds_1h = UNSET
        elif isinstance(self.released_slot_seconds_1h, WaitSlotSeconds):
            released_slot_seconds_1h = self.released_slot_seconds_1h.to_dict()
        else:
            released_slot_seconds_1h = self.released_slot_seconds_1h

        released_slot_seconds_24h: dict[str, Any] | None | Unset
        if isinstance(self.released_slot_seconds_24h, Unset):
            released_slot_seconds_24h = UNSET
        elif isinstance(self.released_slot_seconds_24h, WaitSlotSeconds):
            released_slot_seconds_24h = self.released_slot_seconds_24h.to_dict()
        else:
            released_slot_seconds_24h = self.released_slot_seconds_24h

        evicted_1h: float | None | Unset
        if isinstance(self.evicted_1h, Unset):
            evicted_1h = UNSET
        else:
            evicted_1h = self.evicted_1h

        ceiling_1h: float | None | Unset
        if isinstance(self.ceiling_1h, Unset):
            ceiling_1h = UNSET
        else:
            ceiling_1h = self.ceiling_1h

        resume_cost_p50: float | None | Unset
        if isinstance(self.resume_cost_p50, Unset):
            resume_cost_p50 = UNSET
        else:
            resume_cost_p50 = self.resume_cost_p50

        resume_cost_p95: float | None | Unset
        if isinstance(self.resume_cost_p95, Unset):
            resume_cost_p95 = UNSET
        else:
            resume_cost_p95 = self.resume_cost_p95

        by_project: dict[str, Any] | Unset = UNSET
        if not isinstance(self.by_project, Unset):
            by_project = self.by_project.to_dict()

        wakes_1h: dict[str, Any] | None | Unset
        if isinstance(self.wakes_1h, Unset):
            wakes_1h = UNSET
        elif isinstance(self.wakes_1h, WaitWakes):
            wakes_1h = self.wakes_1h.to_dict()
        else:
            wakes_1h = self.wakes_1h

        loops_1h: float | None | Unset
        if isinstance(self.loops_1h, Unset):
            loops_1h = UNSET
        else:
            loops_1h = self.loops_1h

        escalations_1h: float | None | Unset
        if isinstance(self.escalations_1h, Unset):
            escalations_1h = UNSET
        else:
            escalations_1h = self.escalations_1h

        decisions_1h: float | None | Unset
        if isinstance(self.decisions_1h, Unset):
            decisions_1h = UNSET
        else:
            decisions_1h = self.decisions_1h

        decision_seconds_p50: float | None | Unset
        if isinstance(self.decision_seconds_p50, Unset):
            decision_seconds_p50 = UNSET
        else:
            decision_seconds_p50 = self.decision_seconds_p50

        human_pending: float | None | Unset
        if isinstance(self.human_pending, Unset):
            human_pending = UNSET
        else:
            human_pending = self.human_pending

        human_age_max_seconds: float | None | Unset
        if isinstance(self.human_age_max_seconds, Unset):
            human_age_max_seconds = UNSET
        else:
            human_age_max_seconds = self.human_age_max_seconds

        priority: dict[str, Any] | None | Unset
        if isinstance(self.priority, Unset):
            priority = UNSET
        elif isinstance(self.priority, WaitPriorityMetrics):
            priority = self.priority.to_dict()
        else:
            priority = self.priority

        wait_seconds_1h: float | None | Unset
        if isinstance(self.wait_seconds_1h, Unset):
            wait_seconds_1h = UNSET
        else:
            wait_seconds_1h = self.wait_seconds_1h

        overhead_tokens_1h: float | None | Unset
        if isinstance(self.overhead_tokens_1h, Unset):
            overhead_tokens_1h = UNSET
        else:
            overhead_tokens_1h = self.overhead_tokens_1h

        tokens_saved_counterfactual: dict[str, Any] | None | Unset
        if isinstance(self.tokens_saved_counterfactual, Unset):
            tokens_saved_counterfactual = UNSET
        elif isinstance(self.tokens_saved_counterfactual, WaitCounterfactual):
            tokens_saved_counterfactual = self.tokens_saved_counterfactual.to_dict()
        else:
            tokens_saved_counterfactual = self.tokens_saved_counterfactual

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if pending_in_place is not UNSET:
            field_dict["pending_in_place"] = pending_in_place
        if pending_hibernated is not UNSET:
            field_dict["pending_hibernated"] = pending_hibernated
        if pending_human is not UNSET:
            field_dict["pending_human"] = pending_human
        if escalated is not UNSET:
            field_dict["escalated"] = escalated
        if scheduler_by_reason is not UNSET:
            field_dict["scheduler_by_reason"] = scheduler_by_reason
        if in_place_slot_seconds_1h is not UNSET:
            field_dict["in_place_slot_seconds_1h"] = in_place_slot_seconds_1h
        if released_slot_seconds_1h is not UNSET:
            field_dict["released_slot_seconds_1h"] = released_slot_seconds_1h
        if released_slot_seconds_24h is not UNSET:
            field_dict["released_slot_seconds_24h"] = released_slot_seconds_24h
        if evicted_1h is not UNSET:
            field_dict["evicted_1h"] = evicted_1h
        if ceiling_1h is not UNSET:
            field_dict["ceiling_1h"] = ceiling_1h
        if resume_cost_p50 is not UNSET:
            field_dict["resume_cost_p50"] = resume_cost_p50
        if resume_cost_p95 is not UNSET:
            field_dict["resume_cost_p95"] = resume_cost_p95
        if by_project is not UNSET:
            field_dict["by_project"] = by_project
        if wakes_1h is not UNSET:
            field_dict["wakes_1h"] = wakes_1h
        if loops_1h is not UNSET:
            field_dict["loops_1h"] = loops_1h
        if escalations_1h is not UNSET:
            field_dict["escalations_1h"] = escalations_1h
        if decisions_1h is not UNSET:
            field_dict["decisions_1h"] = decisions_1h
        if decision_seconds_p50 is not UNSET:
            field_dict["decision_seconds_p50"] = decision_seconds_p50
        if human_pending is not UNSET:
            field_dict["human_pending"] = human_pending
        if human_age_max_seconds is not UNSET:
            field_dict["human_age_max_seconds"] = human_age_max_seconds
        if priority is not UNSET:
            field_dict["priority"] = priority
        if wait_seconds_1h is not UNSET:
            field_dict["wait_seconds_1h"] = wait_seconds_1h
        if overhead_tokens_1h is not UNSET:
            field_dict["overhead_tokens_1h"] = overhead_tokens_1h
        if tokens_saved_counterfactual is not UNSET:
            field_dict["tokens_saved_counterfactual"] = tokens_saved_counterfactual

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_counterfactual import WaitCounterfactual
        from ..models.wait_metrics_by_project import WaitMetricsByProject
        from ..models.wait_metrics_scheduler_by_reason import WaitMetricsSchedulerByReason
        from ..models.wait_priority_metrics import WaitPriorityMetrics
        from ..models.wait_slot_seconds import WaitSlotSeconds
        from ..models.wait_wakes import WaitWakes

        d = dict(src_dict)

        def _parse_pending_in_place(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        pending_in_place = _parse_pending_in_place(d.pop("pending_in_place", UNSET))

        def _parse_pending_hibernated(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        pending_hibernated = _parse_pending_hibernated(d.pop("pending_hibernated", UNSET))

        def _parse_pending_human(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        pending_human = _parse_pending_human(d.pop("pending_human", UNSET))

        def _parse_escalated(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        escalated = _parse_escalated(d.pop("escalated", UNSET))

        _scheduler_by_reason = d.pop("scheduler_by_reason", UNSET)
        scheduler_by_reason: WaitMetricsSchedulerByReason | Unset
        if isinstance(_scheduler_by_reason, Unset):
            scheduler_by_reason = UNSET
        else:
            scheduler_by_reason = WaitMetricsSchedulerByReason.from_dict(_scheduler_by_reason)

        def _parse_in_place_slot_seconds_1h(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        in_place_slot_seconds_1h = _parse_in_place_slot_seconds_1h(d.pop("in_place_slot_seconds_1h", UNSET))

        def _parse_released_slot_seconds_1h(data: object) -> None | Unset | WaitSlotSeconds:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                released_slot_seconds_1h_type_0 = WaitSlotSeconds.from_dict(data)

                return released_slot_seconds_1h_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitSlotSeconds, data)

        released_slot_seconds_1h = _parse_released_slot_seconds_1h(d.pop("released_slot_seconds_1h", UNSET))

        def _parse_released_slot_seconds_24h(data: object) -> None | Unset | WaitSlotSeconds:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                released_slot_seconds_24h_type_0 = WaitSlotSeconds.from_dict(data)

                return released_slot_seconds_24h_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitSlotSeconds, data)

        released_slot_seconds_24h = _parse_released_slot_seconds_24h(d.pop("released_slot_seconds_24h", UNSET))

        def _parse_evicted_1h(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        evicted_1h = _parse_evicted_1h(d.pop("evicted_1h", UNSET))

        def _parse_ceiling_1h(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        ceiling_1h = _parse_ceiling_1h(d.pop("ceiling_1h", UNSET))

        def _parse_resume_cost_p50(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        resume_cost_p50 = _parse_resume_cost_p50(d.pop("resume_cost_p50", UNSET))

        def _parse_resume_cost_p95(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        resume_cost_p95 = _parse_resume_cost_p95(d.pop("resume_cost_p95", UNSET))

        _by_project = d.pop("by_project", UNSET)
        by_project: WaitMetricsByProject | Unset
        if isinstance(_by_project, Unset):
            by_project = UNSET
        else:
            by_project = WaitMetricsByProject.from_dict(_by_project)

        def _parse_wakes_1h(data: object) -> None | Unset | WaitWakes:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                wakes_1h_type_0 = WaitWakes.from_dict(data)

                return wakes_1h_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitWakes, data)

        wakes_1h = _parse_wakes_1h(d.pop("wakes_1h", UNSET))

        def _parse_loops_1h(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        loops_1h = _parse_loops_1h(d.pop("loops_1h", UNSET))

        def _parse_escalations_1h(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        escalations_1h = _parse_escalations_1h(d.pop("escalations_1h", UNSET))

        def _parse_decisions_1h(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        decisions_1h = _parse_decisions_1h(d.pop("decisions_1h", UNSET))

        def _parse_decision_seconds_p50(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        decision_seconds_p50 = _parse_decision_seconds_p50(d.pop("decision_seconds_p50", UNSET))

        def _parse_human_pending(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        human_pending = _parse_human_pending(d.pop("human_pending", UNSET))

        def _parse_human_age_max_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        human_age_max_seconds = _parse_human_age_max_seconds(d.pop("human_age_max_seconds", UNSET))

        def _parse_priority(data: object) -> None | Unset | WaitPriorityMetrics:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                priority_type_0 = WaitPriorityMetrics.from_dict(data)

                return priority_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitPriorityMetrics, data)

        priority = _parse_priority(d.pop("priority", UNSET))

        def _parse_wait_seconds_1h(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        wait_seconds_1h = _parse_wait_seconds_1h(d.pop("wait_seconds_1h", UNSET))

        def _parse_overhead_tokens_1h(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        overhead_tokens_1h = _parse_overhead_tokens_1h(d.pop("overhead_tokens_1h", UNSET))

        def _parse_tokens_saved_counterfactual(data: object) -> None | Unset | WaitCounterfactual:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                tokens_saved_counterfactual_type_0 = WaitCounterfactual.from_dict(data)

                return tokens_saved_counterfactual_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitCounterfactual, data)

        tokens_saved_counterfactual = _parse_tokens_saved_counterfactual(d.pop("tokens_saved_counterfactual", UNSET))

        wait_metrics = cls(
            pending_in_place=pending_in_place,
            pending_hibernated=pending_hibernated,
            pending_human=pending_human,
            escalated=escalated,
            scheduler_by_reason=scheduler_by_reason,
            in_place_slot_seconds_1h=in_place_slot_seconds_1h,
            released_slot_seconds_1h=released_slot_seconds_1h,
            released_slot_seconds_24h=released_slot_seconds_24h,
            evicted_1h=evicted_1h,
            ceiling_1h=ceiling_1h,
            resume_cost_p50=resume_cost_p50,
            resume_cost_p95=resume_cost_p95,
            by_project=by_project,
            wakes_1h=wakes_1h,
            loops_1h=loops_1h,
            escalations_1h=escalations_1h,
            decisions_1h=decisions_1h,
            decision_seconds_p50=decision_seconds_p50,
            human_pending=human_pending,
            human_age_max_seconds=human_age_max_seconds,
            priority=priority,
            wait_seconds_1h=wait_seconds_1h,
            overhead_tokens_1h=overhead_tokens_1h,
            tokens_saved_counterfactual=tokens_saved_counterfactual,
        )

        wait_metrics.additional_properties = d
        return wait_metrics

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
