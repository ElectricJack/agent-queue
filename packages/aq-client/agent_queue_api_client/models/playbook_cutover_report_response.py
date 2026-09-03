from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.playbook_cutover_report_response_acknowledged_disabled_item import (
        PlaybookCutoverReportResponseAcknowledgedDisabledItem,
    )
    from ..models.playbook_cutover_report_response_active_v1_runs import PlaybookCutoverReportResponseActiveV1Runs
    from ..models.playbook_cutover_report_response_artifacts_item import PlaybookCutoverReportResponseArtifactsItem
    from ..models.playbook_cutover_report_response_parity import PlaybookCutoverReportResponseParity
    from ..models.playbook_cutover_report_response_pending_events import PlaybookCutoverReportResponsePendingEvents
    from ..models.playbook_cutover_report_response_unresolved_item import PlaybookCutoverReportResponseUnresolvedItem


T = TypeVar("T", bound="PlaybookCutoverReportResponse")


@_attrs_define
class PlaybookCutoverReportResponse:
    """Read-only, signed-operator-ready evidence for the V1→V2 cutover.

    Attributes:
        success (bool):
        generated_at (float):
        contract_fingerprint (str):
        pending_events (PlaybookCutoverReportResponsePendingEvents):
        active_v1_runs (PlaybookCutoverReportResponseActiveV1Runs):
        parity (PlaybookCutoverReportResponseParity):
        rollback_ready (bool):
        cutover_eligible (bool):
        artifacts (list[PlaybookCutoverReportResponseArtifactsItem] | Unset):
        unresolved (list[PlaybookCutoverReportResponseUnresolvedItem] | Unset):
        acknowledged_disabled (list[PlaybookCutoverReportResponseAcknowledgedDisabledItem] | Unset):
        blocking_reasons (list[str] | Unset):
    """

    success: bool
    generated_at: float
    contract_fingerprint: str
    pending_events: PlaybookCutoverReportResponsePendingEvents
    active_v1_runs: PlaybookCutoverReportResponseActiveV1Runs
    parity: PlaybookCutoverReportResponseParity
    rollback_ready: bool
    cutover_eligible: bool
    artifacts: list[PlaybookCutoverReportResponseArtifactsItem] | Unset = UNSET
    unresolved: list[PlaybookCutoverReportResponseUnresolvedItem] | Unset = UNSET
    acknowledged_disabled: list[PlaybookCutoverReportResponseAcknowledgedDisabledItem] | Unset = UNSET
    blocking_reasons: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        generated_at = self.generated_at

        contract_fingerprint = self.contract_fingerprint

        pending_events = self.pending_events.to_dict()

        active_v1_runs = self.active_v1_runs.to_dict()

        parity = self.parity.to_dict()

        rollback_ready = self.rollback_ready

        cutover_eligible = self.cutover_eligible

        artifacts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.artifacts, Unset):
            artifacts = []
            for artifacts_item_data in self.artifacts:
                artifacts_item = artifacts_item_data.to_dict()
                artifacts.append(artifacts_item)

        unresolved: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.unresolved, Unset):
            unresolved = []
            for unresolved_item_data in self.unresolved:
                unresolved_item = unresolved_item_data.to_dict()
                unresolved.append(unresolved_item)

        acknowledged_disabled: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.acknowledged_disabled, Unset):
            acknowledged_disabled = []
            for acknowledged_disabled_item_data in self.acknowledged_disabled:
                acknowledged_disabled_item = acknowledged_disabled_item_data.to_dict()
                acknowledged_disabled.append(acknowledged_disabled_item)

        blocking_reasons: list[str] | Unset = UNSET
        if not isinstance(self.blocking_reasons, Unset):
            blocking_reasons = self.blocking_reasons

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "success": success,
                "generated_at": generated_at,
                "contract_fingerprint": contract_fingerprint,
                "pending_events": pending_events,
                "active_v1_runs": active_v1_runs,
                "parity": parity,
                "rollback_ready": rollback_ready,
                "cutover_eligible": cutover_eligible,
            }
        )
        if artifacts is not UNSET:
            field_dict["artifacts"] = artifacts
        if unresolved is not UNSET:
            field_dict["unresolved"] = unresolved
        if acknowledged_disabled is not UNSET:
            field_dict["acknowledged_disabled"] = acknowledged_disabled
        if blocking_reasons is not UNSET:
            field_dict["blocking_reasons"] = blocking_reasons

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.playbook_cutover_report_response_acknowledged_disabled_item import (
            PlaybookCutoverReportResponseAcknowledgedDisabledItem,
        )
        from ..models.playbook_cutover_report_response_active_v1_runs import PlaybookCutoverReportResponseActiveV1Runs
        from ..models.playbook_cutover_report_response_artifacts_item import PlaybookCutoverReportResponseArtifactsItem
        from ..models.playbook_cutover_report_response_parity import PlaybookCutoverReportResponseParity
        from ..models.playbook_cutover_report_response_pending_events import PlaybookCutoverReportResponsePendingEvents
        from ..models.playbook_cutover_report_response_unresolved_item import (
            PlaybookCutoverReportResponseUnresolvedItem,
        )

        d = dict(src_dict)
        success = d.pop("success")

        generated_at = d.pop("generated_at")

        contract_fingerprint = d.pop("contract_fingerprint")

        pending_events = PlaybookCutoverReportResponsePendingEvents.from_dict(d.pop("pending_events"))

        active_v1_runs = PlaybookCutoverReportResponseActiveV1Runs.from_dict(d.pop("active_v1_runs"))

        parity = PlaybookCutoverReportResponseParity.from_dict(d.pop("parity"))

        rollback_ready = d.pop("rollback_ready")

        cutover_eligible = d.pop("cutover_eligible")

        _artifacts = d.pop("artifacts", UNSET)
        artifacts: list[PlaybookCutoverReportResponseArtifactsItem] | Unset = UNSET
        if _artifacts is not UNSET:
            artifacts = []
            for artifacts_item_data in _artifacts:
                artifacts_item = PlaybookCutoverReportResponseArtifactsItem.from_dict(artifacts_item_data)

                artifacts.append(artifacts_item)

        _unresolved = d.pop("unresolved", UNSET)
        unresolved: list[PlaybookCutoverReportResponseUnresolvedItem] | Unset = UNSET
        if _unresolved is not UNSET:
            unresolved = []
            for unresolved_item_data in _unresolved:
                unresolved_item = PlaybookCutoverReportResponseUnresolvedItem.from_dict(unresolved_item_data)

                unresolved.append(unresolved_item)

        _acknowledged_disabled = d.pop("acknowledged_disabled", UNSET)
        acknowledged_disabled: list[PlaybookCutoverReportResponseAcknowledgedDisabledItem] | Unset = UNSET
        if _acknowledged_disabled is not UNSET:
            acknowledged_disabled = []
            for acknowledged_disabled_item_data in _acknowledged_disabled:
                acknowledged_disabled_item = PlaybookCutoverReportResponseAcknowledgedDisabledItem.from_dict(
                    acknowledged_disabled_item_data
                )

                acknowledged_disabled.append(acknowledged_disabled_item)

        blocking_reasons = cast(list[str], d.pop("blocking_reasons", UNSET))

        playbook_cutover_report_response = cls(
            success=success,
            generated_at=generated_at,
            contract_fingerprint=contract_fingerprint,
            pending_events=pending_events,
            active_v1_runs=active_v1_runs,
            parity=parity,
            rollback_ready=rollback_ready,
            cutover_eligible=cutover_eligible,
            artifacts=artifacts,
            unresolved=unresolved,
            acknowledged_disabled=acknowledged_disabled,
            blocking_reasons=blocking_reasons,
        )

        return playbook_cutover_report_response
