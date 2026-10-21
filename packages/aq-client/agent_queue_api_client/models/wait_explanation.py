from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.wait_chain_edge import WaitChainEdge
    from ..models.wait_deadline import WaitDeadline
    from ..models.wait_effective_priority import WaitEffectivePriority
    from ..models.wait_explanation_continuation_type_0 import WaitExplanationContinuationType0
    from ..models.wait_explanation_edge_history_item import WaitExplanationEdgeHistoryItem
    from ..models.wait_explanation_escalation_type_0 import WaitExplanationEscalationType0
    from ..models.wait_explanation_escalations_item import WaitExplanationEscalationsItem
    from ..models.wait_explanation_owner import WaitExplanationOwner
    from ..models.wait_explanation_reservations_item import WaitExplanationReservationsItem
    from ..models.wait_sleep_explanation import WaitSleepExplanation
    from ..models.wait_verified_evidence import WaitVerifiedEvidence


T = TypeVar("T", bound="WaitExplanation")


@_attrs_define
class WaitExplanation:
    """
    Attributes:
        wait_id (None | str | Unset):
        project_id (None | str | Unset):
        owner (WaitExplanationOwner | Unset):
        lifecycle (None | str | Unset):
        outcome (None | str | Unset):
        result_ref (None | str | Unset):
        age_seconds (float | None | Unset):
        age_unknown (bool | Unset):  Default: False.
        verified (WaitVerifiedEvidence | Unset): AQ observations only; claimed commentary is never a source fact.
        claimed (None | str | Unset):
        sleep (WaitSleepExplanation | Unset):
        deadlines (list[WaitDeadline] | Unset):
        chain (list[WaitChainEdge] | Unset):
        chain_truncated (bool | Unset):  Default: False.
        chain_at (float | None | Unset):
        edge_history (list[WaitExplanationEdgeHistoryItem] | Unset):
        effective_priority (WaitEffectivePriority | Unset):
        escalation (None | Unset | WaitExplanationEscalationType0):
        escalations (list[WaitExplanationEscalationsItem] | Unset):
        reservations (list[WaitExplanationReservationsItem] | Unset):
        continuation (None | Unset | WaitExplanationContinuationType0):
        policy_layer (str | Unset):  Default: 'default'.
    """

    wait_id: None | str | Unset = UNSET
    project_id: None | str | Unset = UNSET
    owner: WaitExplanationOwner | Unset = UNSET
    lifecycle: None | str | Unset = UNSET
    outcome: None | str | Unset = UNSET
    result_ref: None | str | Unset = UNSET
    age_seconds: float | None | Unset = UNSET
    age_unknown: bool | Unset = False
    verified: WaitVerifiedEvidence | Unset = UNSET
    claimed: None | str | Unset = UNSET
    sleep: WaitSleepExplanation | Unset = UNSET
    deadlines: list[WaitDeadline] | Unset = UNSET
    chain: list[WaitChainEdge] | Unset = UNSET
    chain_truncated: bool | Unset = False
    chain_at: float | None | Unset = UNSET
    edge_history: list[WaitExplanationEdgeHistoryItem] | Unset = UNSET
    effective_priority: WaitEffectivePriority | Unset = UNSET
    escalation: None | Unset | WaitExplanationEscalationType0 = UNSET
    escalations: list[WaitExplanationEscalationsItem] | Unset = UNSET
    reservations: list[WaitExplanationReservationsItem] | Unset = UNSET
    continuation: None | Unset | WaitExplanationContinuationType0 = UNSET
    policy_layer: str | Unset = "default"
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)

    def to_dict(self) -> dict[str, Any]:
        from ..models.wait_explanation_continuation_type_0 import WaitExplanationContinuationType0
        from ..models.wait_explanation_escalation_type_0 import WaitExplanationEscalationType0

        wait_id: None | str | Unset
        if isinstance(self.wait_id, Unset):
            wait_id = UNSET
        else:
            wait_id = self.wait_id

        project_id: None | str | Unset
        if isinstance(self.project_id, Unset):
            project_id = UNSET
        else:
            project_id = self.project_id

        owner: dict[str, Any] | Unset = UNSET
        if not isinstance(self.owner, Unset):
            owner = self.owner.to_dict()

        lifecycle: None | str | Unset
        if isinstance(self.lifecycle, Unset):
            lifecycle = UNSET
        else:
            lifecycle = self.lifecycle

        outcome: None | str | Unset
        if isinstance(self.outcome, Unset):
            outcome = UNSET
        else:
            outcome = self.outcome

        result_ref: None | str | Unset
        if isinstance(self.result_ref, Unset):
            result_ref = UNSET
        else:
            result_ref = self.result_ref

        age_seconds: float | None | Unset
        if isinstance(self.age_seconds, Unset):
            age_seconds = UNSET
        else:
            age_seconds = self.age_seconds

        age_unknown = self.age_unknown

        verified: dict[str, Any] | Unset = UNSET
        if not isinstance(self.verified, Unset):
            verified = self.verified.to_dict()

        claimed: None | str | Unset
        if isinstance(self.claimed, Unset):
            claimed = UNSET
        else:
            claimed = self.claimed

        sleep: dict[str, Any] | Unset = UNSET
        if not isinstance(self.sleep, Unset):
            sleep = self.sleep.to_dict()

        deadlines: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.deadlines, Unset):
            deadlines = []
            for deadlines_item_data in self.deadlines:
                deadlines_item = deadlines_item_data.to_dict()
                deadlines.append(deadlines_item)

        chain: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.chain, Unset):
            chain = []
            for chain_item_data in self.chain:
                chain_item = chain_item_data.to_dict()
                chain.append(chain_item)

        chain_truncated = self.chain_truncated

        chain_at: float | None | Unset
        if isinstance(self.chain_at, Unset):
            chain_at = UNSET
        else:
            chain_at = self.chain_at

        edge_history: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.edge_history, Unset):
            edge_history = []
            for edge_history_item_data in self.edge_history:
                edge_history_item = edge_history_item_data.to_dict()
                edge_history.append(edge_history_item)

        effective_priority: dict[str, Any] | Unset = UNSET
        if not isinstance(self.effective_priority, Unset):
            effective_priority = self.effective_priority.to_dict()

        escalation: dict[str, Any] | None | Unset
        if isinstance(self.escalation, Unset):
            escalation = UNSET
        elif isinstance(self.escalation, WaitExplanationEscalationType0):
            escalation = self.escalation.to_dict()
        else:
            escalation = self.escalation

        escalations: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.escalations, Unset):
            escalations = []
            for escalations_item_data in self.escalations:
                escalations_item = escalations_item_data.to_dict()
                escalations.append(escalations_item)

        reservations: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.reservations, Unset):
            reservations = []
            for reservations_item_data in self.reservations:
                reservations_item = reservations_item_data.to_dict()
                reservations.append(reservations_item)

        continuation: dict[str, Any] | None | Unset
        if isinstance(self.continuation, Unset):
            continuation = UNSET
        elif isinstance(self.continuation, WaitExplanationContinuationType0):
            continuation = self.continuation.to_dict()
        else:
            continuation = self.continuation

        policy_layer = self.policy_layer

        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({})
        if wait_id is not UNSET:
            field_dict["wait_id"] = wait_id
        if project_id is not UNSET:
            field_dict["project_id"] = project_id
        if owner is not UNSET:
            field_dict["owner"] = owner
        if lifecycle is not UNSET:
            field_dict["lifecycle"] = lifecycle
        if outcome is not UNSET:
            field_dict["outcome"] = outcome
        if result_ref is not UNSET:
            field_dict["result_ref"] = result_ref
        if age_seconds is not UNSET:
            field_dict["age_seconds"] = age_seconds
        if age_unknown is not UNSET:
            field_dict["age_unknown"] = age_unknown
        if verified is not UNSET:
            field_dict["verified"] = verified
        if claimed is not UNSET:
            field_dict["claimed"] = claimed
        if sleep is not UNSET:
            field_dict["sleep"] = sleep
        if deadlines is not UNSET:
            field_dict["deadlines"] = deadlines
        if chain is not UNSET:
            field_dict["chain"] = chain
        if chain_truncated is not UNSET:
            field_dict["chain_truncated"] = chain_truncated
        if chain_at is not UNSET:
            field_dict["chain_at"] = chain_at
        if edge_history is not UNSET:
            field_dict["edge_history"] = edge_history
        if effective_priority is not UNSET:
            field_dict["effective_priority"] = effective_priority
        if escalation is not UNSET:
            field_dict["escalation"] = escalation
        if escalations is not UNSET:
            field_dict["escalations"] = escalations
        if reservations is not UNSET:
            field_dict["reservations"] = reservations
        if continuation is not UNSET:
            field_dict["continuation"] = continuation
        if policy_layer is not UNSET:
            field_dict["policy_layer"] = policy_layer

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.wait_chain_edge import WaitChainEdge
        from ..models.wait_deadline import WaitDeadline
        from ..models.wait_effective_priority import WaitEffectivePriority
        from ..models.wait_explanation_continuation_type_0 import WaitExplanationContinuationType0
        from ..models.wait_explanation_edge_history_item import WaitExplanationEdgeHistoryItem
        from ..models.wait_explanation_escalation_type_0 import WaitExplanationEscalationType0
        from ..models.wait_explanation_escalations_item import WaitExplanationEscalationsItem
        from ..models.wait_explanation_owner import WaitExplanationOwner
        from ..models.wait_explanation_reservations_item import WaitExplanationReservationsItem
        from ..models.wait_sleep_explanation import WaitSleepExplanation
        from ..models.wait_verified_evidence import WaitVerifiedEvidence

        d = dict(src_dict)

        def _parse_wait_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        wait_id = _parse_wait_id(d.pop("wait_id", UNSET))

        def _parse_project_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        project_id = _parse_project_id(d.pop("project_id", UNSET))

        _owner = d.pop("owner", UNSET)
        owner: WaitExplanationOwner | Unset
        if isinstance(_owner, Unset):
            owner = UNSET
        else:
            owner = WaitExplanationOwner.from_dict(_owner)

        def _parse_lifecycle(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        lifecycle = _parse_lifecycle(d.pop("lifecycle", UNSET))

        def _parse_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        outcome = _parse_outcome(d.pop("outcome", UNSET))

        def _parse_result_ref(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        result_ref = _parse_result_ref(d.pop("result_ref", UNSET))

        def _parse_age_seconds(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        age_seconds = _parse_age_seconds(d.pop("age_seconds", UNSET))

        age_unknown = d.pop("age_unknown", UNSET)

        _verified = d.pop("verified", UNSET)
        verified: WaitVerifiedEvidence | Unset
        if isinstance(_verified, Unset):
            verified = UNSET
        else:
            verified = WaitVerifiedEvidence.from_dict(_verified)

        def _parse_claimed(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        claimed = _parse_claimed(d.pop("claimed", UNSET))

        _sleep = d.pop("sleep", UNSET)
        sleep: WaitSleepExplanation | Unset
        if isinstance(_sleep, Unset):
            sleep = UNSET
        else:
            sleep = WaitSleepExplanation.from_dict(_sleep)

        _deadlines = d.pop("deadlines", UNSET)
        deadlines: list[WaitDeadline] | Unset = UNSET
        if _deadlines is not UNSET:
            deadlines = []
            for deadlines_item_data in _deadlines:
                deadlines_item = WaitDeadline.from_dict(deadlines_item_data)

                deadlines.append(deadlines_item)

        _chain = d.pop("chain", UNSET)
        chain: list[WaitChainEdge] | Unset = UNSET
        if _chain is not UNSET:
            chain = []
            for chain_item_data in _chain:
                chain_item = WaitChainEdge.from_dict(chain_item_data)

                chain.append(chain_item)

        chain_truncated = d.pop("chain_truncated", UNSET)

        def _parse_chain_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        chain_at = _parse_chain_at(d.pop("chain_at", UNSET))

        _edge_history = d.pop("edge_history", UNSET)
        edge_history: list[WaitExplanationEdgeHistoryItem] | Unset = UNSET
        if _edge_history is not UNSET:
            edge_history = []
            for edge_history_item_data in _edge_history:
                edge_history_item = WaitExplanationEdgeHistoryItem.from_dict(edge_history_item_data)

                edge_history.append(edge_history_item)

        _effective_priority = d.pop("effective_priority", UNSET)
        effective_priority: WaitEffectivePriority | Unset
        if isinstance(_effective_priority, Unset):
            effective_priority = UNSET
        else:
            effective_priority = WaitEffectivePriority.from_dict(_effective_priority)

        def _parse_escalation(data: object) -> None | Unset | WaitExplanationEscalationType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                escalation_type_0 = WaitExplanationEscalationType0.from_dict(data)

                return escalation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitExplanationEscalationType0, data)

        escalation = _parse_escalation(d.pop("escalation", UNSET))

        _escalations = d.pop("escalations", UNSET)
        escalations: list[WaitExplanationEscalationsItem] | Unset = UNSET
        if _escalations is not UNSET:
            escalations = []
            for escalations_item_data in _escalations:
                escalations_item = WaitExplanationEscalationsItem.from_dict(escalations_item_data)

                escalations.append(escalations_item)

        _reservations = d.pop("reservations", UNSET)
        reservations: list[WaitExplanationReservationsItem] | Unset = UNSET
        if _reservations is not UNSET:
            reservations = []
            for reservations_item_data in _reservations:
                reservations_item = WaitExplanationReservationsItem.from_dict(reservations_item_data)

                reservations.append(reservations_item)

        def _parse_continuation(data: object) -> None | Unset | WaitExplanationContinuationType0:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                continuation_type_0 = WaitExplanationContinuationType0.from_dict(data)

                return continuation_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitExplanationContinuationType0, data)

        continuation = _parse_continuation(d.pop("continuation", UNSET))

        policy_layer = d.pop("policy_layer", UNSET)

        wait_explanation = cls(
            wait_id=wait_id,
            project_id=project_id,
            owner=owner,
            lifecycle=lifecycle,
            outcome=outcome,
            result_ref=result_ref,
            age_seconds=age_seconds,
            age_unknown=age_unknown,
            verified=verified,
            claimed=claimed,
            sleep=sleep,
            deadlines=deadlines,
            chain=chain,
            chain_truncated=chain_truncated,
            chain_at=chain_at,
            edge_history=edge_history,
            effective_priority=effective_priority,
            escalation=escalation,
            escalations=escalations,
            reservations=reservations,
            continuation=continuation,
            policy_layer=policy_layer,
        )

        wait_explanation.additional_properties = d
        return wait_explanation

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
