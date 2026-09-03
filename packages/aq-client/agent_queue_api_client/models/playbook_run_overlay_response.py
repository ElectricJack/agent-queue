from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.playbook_run_overlay_response_lifecycle import PlaybookRunOverlayResponseLifecycle
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.artifact_ref_dto import ArtifactRefDTO
    from ..models.edge_overlay_dto import EdgeOverlayDTO
    from ..models.explanation_row_dto import ExplanationRowDTO
    from ..models.graph_diagnostic_dto import GraphDiagnosticDTO
    from ..models.node_overlay_dto import NodeOverlayDTO
    from ..models.operator_decision_dto import OperatorDecisionDTO
    from ..models.playbook_run_overlay_response_trigger_event import PlaybookRunOverlayResponseTriggerEvent
    from ..models.receipt_dto import ReceiptDTO
    from ..models.run_budget_dto import RunBudgetDTO


T = TypeVar("T", bound="PlaybookRunOverlayResponse")


@_attrs_define
class PlaybookRunOverlayResponse:
    """Carries the artifact ref of the **pinned** artifact and nothing else.
    The dashboard fetches the graph for ``overlay.artifact.artifact_sha256``,
    never for the playbook's current activation; ``artifact_is_active=False``
    renders a persistent banner.  This is the single mechanism satisfying "Run
    overlays are pinned to the exact artifact executed".

        Attributes:
            run_id (str):
            artifact (ArtifactRefDTO): Roadmap §4 ``ArtifactRef``, projected.  Identifies exactly one
                immutable artifact; every graph, diff and overlay response carries one.
            rule_id (str):
            lifecycle (PlaybookRunOverlayResponseLifecycle):
            success (bool | Unset):  Default: True.
            artifact_is_active (bool | Unset):  Default: False.
            current_step_id (None | str | Unset):
            started_at (float | None | Unset):
            completed_at (float | None | Unset):
            deadline_at (float | None | Unset):
            trigger_event (PlaybookRunOverlayResponseTriggerEvent | Unset):
            nodes (list[NodeOverlayDTO] | Unset):
            edges (list[EdgeOverlayDTO] | Unset):
            receipts (list[ReceiptDTO] | Unset):
            bindings (list[ExplanationRowDTO] | Unset):
            operator_decision (None | OperatorDecisionDTO | Unset):
            budget (None | RunBudgetDTO | Unset):
            diagnostics (list[GraphDiagnosticDTO] | Unset):
            truncated (bool | Unset):  Default: False.
            receipt_total (int | Unset):  Default: 0.
    """

    run_id: str
    artifact: ArtifactRefDTO
    rule_id: str
    lifecycle: PlaybookRunOverlayResponseLifecycle
    success: bool | Unset = True
    artifact_is_active: bool | Unset = False
    current_step_id: None | str | Unset = UNSET
    started_at: float | None | Unset = UNSET
    completed_at: float | None | Unset = UNSET
    deadline_at: float | None | Unset = UNSET
    trigger_event: PlaybookRunOverlayResponseTriggerEvent | Unset = UNSET
    nodes: list[NodeOverlayDTO] | Unset = UNSET
    edges: list[EdgeOverlayDTO] | Unset = UNSET
    receipts: list[ReceiptDTO] | Unset = UNSET
    bindings: list[ExplanationRowDTO] | Unset = UNSET
    operator_decision: None | OperatorDecisionDTO | Unset = UNSET
    budget: None | RunBudgetDTO | Unset = UNSET
    diagnostics: list[GraphDiagnosticDTO] | Unset = UNSET
    truncated: bool | Unset = False
    receipt_total: int | Unset = 0

    def to_dict(self) -> dict[str, Any]:
        from ..models.operator_decision_dto import OperatorDecisionDTO
        from ..models.run_budget_dto import RunBudgetDTO

        run_id = self.run_id

        artifact = self.artifact.to_dict()

        rule_id = self.rule_id

        lifecycle = self.lifecycle.value

        success = self.success

        artifact_is_active = self.artifact_is_active

        current_step_id: None | str | Unset
        if isinstance(self.current_step_id, Unset):
            current_step_id = UNSET
        else:
            current_step_id = self.current_step_id

        started_at: float | None | Unset
        if isinstance(self.started_at, Unset):
            started_at = UNSET
        else:
            started_at = self.started_at

        completed_at: float | None | Unset
        if isinstance(self.completed_at, Unset):
            completed_at = UNSET
        else:
            completed_at = self.completed_at

        deadline_at: float | None | Unset
        if isinstance(self.deadline_at, Unset):
            deadline_at = UNSET
        else:
            deadline_at = self.deadline_at

        trigger_event: dict[str, Any] | Unset = UNSET
        if not isinstance(self.trigger_event, Unset):
            trigger_event = self.trigger_event.to_dict()

        nodes: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.nodes, Unset):
            nodes = []
            for nodes_item_data in self.nodes:
                nodes_item = nodes_item_data.to_dict()
                nodes.append(nodes_item)

        edges: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.edges, Unset):
            edges = []
            for edges_item_data in self.edges:
                edges_item = edges_item_data.to_dict()
                edges.append(edges_item)

        receipts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.receipts, Unset):
            receipts = []
            for receipts_item_data in self.receipts:
                receipts_item = receipts_item_data.to_dict()
                receipts.append(receipts_item)

        bindings: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.bindings, Unset):
            bindings = []
            for bindings_item_data in self.bindings:
                bindings_item = bindings_item_data.to_dict()
                bindings.append(bindings_item)

        operator_decision: dict[str, Any] | None | Unset
        if isinstance(self.operator_decision, Unset):
            operator_decision = UNSET
        elif isinstance(self.operator_decision, OperatorDecisionDTO):
            operator_decision = self.operator_decision.to_dict()
        else:
            operator_decision = self.operator_decision

        budget: dict[str, Any] | None | Unset
        if isinstance(self.budget, Unset):
            budget = UNSET
        elif isinstance(self.budget, RunBudgetDTO):
            budget = self.budget.to_dict()
        else:
            budget = self.budget

        diagnostics: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.diagnostics, Unset):
            diagnostics = []
            for diagnostics_item_data in self.diagnostics:
                diagnostics_item = diagnostics_item_data.to_dict()
                diagnostics.append(diagnostics_item)

        truncated = self.truncated

        receipt_total = self.receipt_total

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "run_id": run_id,
                "artifact": artifact,
                "rule_id": rule_id,
                "lifecycle": lifecycle,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if artifact_is_active is not UNSET:
            field_dict["artifact_is_active"] = artifact_is_active
        if current_step_id is not UNSET:
            field_dict["current_step_id"] = current_step_id
        if started_at is not UNSET:
            field_dict["started_at"] = started_at
        if completed_at is not UNSET:
            field_dict["completed_at"] = completed_at
        if deadline_at is not UNSET:
            field_dict["deadline_at"] = deadline_at
        if trigger_event is not UNSET:
            field_dict["trigger_event"] = trigger_event
        if nodes is not UNSET:
            field_dict["nodes"] = nodes
        if edges is not UNSET:
            field_dict["edges"] = edges
        if receipts is not UNSET:
            field_dict["receipts"] = receipts
        if bindings is not UNSET:
            field_dict["bindings"] = bindings
        if operator_decision is not UNSET:
            field_dict["operator_decision"] = operator_decision
        if budget is not UNSET:
            field_dict["budget"] = budget
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics
        if truncated is not UNSET:
            field_dict["truncated"] = truncated
        if receipt_total is not UNSET:
            field_dict["receipt_total"] = receipt_total

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_ref_dto import ArtifactRefDTO
        from ..models.edge_overlay_dto import EdgeOverlayDTO
        from ..models.explanation_row_dto import ExplanationRowDTO
        from ..models.graph_diagnostic_dto import GraphDiagnosticDTO
        from ..models.node_overlay_dto import NodeOverlayDTO
        from ..models.operator_decision_dto import OperatorDecisionDTO
        from ..models.playbook_run_overlay_response_trigger_event import PlaybookRunOverlayResponseTriggerEvent
        from ..models.receipt_dto import ReceiptDTO
        from ..models.run_budget_dto import RunBudgetDTO

        d = dict(src_dict)
        run_id = d.pop("run_id")

        artifact = ArtifactRefDTO.from_dict(d.pop("artifact"))

        rule_id = d.pop("rule_id")

        lifecycle = PlaybookRunOverlayResponseLifecycle(d.pop("lifecycle"))

        success = d.pop("success", UNSET)

        artifact_is_active = d.pop("artifact_is_active", UNSET)

        def _parse_current_step_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        current_step_id = _parse_current_step_id(d.pop("current_step_id", UNSET))

        def _parse_started_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        started_at = _parse_started_at(d.pop("started_at", UNSET))

        def _parse_completed_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        completed_at = _parse_completed_at(d.pop("completed_at", UNSET))

        def _parse_deadline_at(data: object) -> float | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(float | None | Unset, data)

        deadline_at = _parse_deadline_at(d.pop("deadline_at", UNSET))

        _trigger_event = d.pop("trigger_event", UNSET)
        trigger_event: PlaybookRunOverlayResponseTriggerEvent | Unset
        if isinstance(_trigger_event, Unset):
            trigger_event = UNSET
        else:
            trigger_event = PlaybookRunOverlayResponseTriggerEvent.from_dict(_trigger_event)

        _nodes = d.pop("nodes", UNSET)
        nodes: list[NodeOverlayDTO] | Unset = UNSET
        if _nodes is not UNSET:
            nodes = []
            for nodes_item_data in _nodes:
                nodes_item = NodeOverlayDTO.from_dict(nodes_item_data)

                nodes.append(nodes_item)

        _edges = d.pop("edges", UNSET)
        edges: list[EdgeOverlayDTO] | Unset = UNSET
        if _edges is not UNSET:
            edges = []
            for edges_item_data in _edges:
                edges_item = EdgeOverlayDTO.from_dict(edges_item_data)

                edges.append(edges_item)

        _receipts = d.pop("receipts", UNSET)
        receipts: list[ReceiptDTO] | Unset = UNSET
        if _receipts is not UNSET:
            receipts = []
            for receipts_item_data in _receipts:
                receipts_item = ReceiptDTO.from_dict(receipts_item_data)

                receipts.append(receipts_item)

        _bindings = d.pop("bindings", UNSET)
        bindings: list[ExplanationRowDTO] | Unset = UNSET
        if _bindings is not UNSET:
            bindings = []
            for bindings_item_data in _bindings:
                bindings_item = ExplanationRowDTO.from_dict(bindings_item_data)

                bindings.append(bindings_item)

        def _parse_operator_decision(data: object) -> None | OperatorDecisionDTO | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                operator_decision_type_0 = OperatorDecisionDTO.from_dict(data)

                return operator_decision_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperatorDecisionDTO | Unset, data)

        operator_decision = _parse_operator_decision(d.pop("operator_decision", UNSET))

        def _parse_budget(data: object) -> None | RunBudgetDTO | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                budget_type_0 = RunBudgetDTO.from_dict(data)

                return budget_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RunBudgetDTO | Unset, data)

        budget = _parse_budget(d.pop("budget", UNSET))

        _diagnostics = d.pop("diagnostics", UNSET)
        diagnostics: list[GraphDiagnosticDTO] | Unset = UNSET
        if _diagnostics is not UNSET:
            diagnostics = []
            for diagnostics_item_data in _diagnostics:
                diagnostics_item = GraphDiagnosticDTO.from_dict(diagnostics_item_data)

                diagnostics.append(diagnostics_item)

        truncated = d.pop("truncated", UNSET)

        receipt_total = d.pop("receipt_total", UNSET)

        playbook_run_overlay_response = cls(
            run_id=run_id,
            artifact=artifact,
            rule_id=rule_id,
            lifecycle=lifecycle,
            success=success,
            artifact_is_active=artifact_is_active,
            current_step_id=current_step_id,
            started_at=started_at,
            completed_at=completed_at,
            deadline_at=deadline_at,
            trigger_event=trigger_event,
            nodes=nodes,
            edges=edges,
            receipts=receipts,
            bindings=bindings,
            operator_decision=operator_decision,
            budget=budget,
            diagnostics=diagnostics,
            truncated=truncated,
            receipt_total=receipt_total,
        )

        return playbook_run_overlay_response
