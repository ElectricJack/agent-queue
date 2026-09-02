from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.artifact_ref_dto import ArtifactRefDTO
    from ..models.contract_change_dto import ContractChangeDTO
    from ..models.edge_diff_dto import EdgeDiffDTO
    from ..models.graph_diagnostic_dto import GraphDiagnosticDTO
    from ..models.rule_diff_dto import RuleDiffDTO
    from ..models.step_diff_dto import StepDiffDTO


T = TypeVar("T", bound="PlaybookArtifactDiffResponse")


@_attrs_define
class PlaybookArtifactDiffResponse:
    """``executable_change=False`` with ``presentation_change_count>0`` is the
    spec's "a label or help-text improvement does not block activation or change
    an execution fingerprint".  The diff is computed from the two
    ``PlaybookDefinition`` objects, **not** from their JSON bytes.

        Attributes:
            target (ArtifactRefDTO): Roadmap §4 ``ArtifactRef``, projected.  Identifies exactly one
                immutable artifact; every graph, diff and overlay response carries one.
            success (bool | Unset):  Default: True.
            base (ArtifactRefDTO | None | Unset):
            executable_change (bool | Unset):  Default: False.
            semantic_change_count (int | Unset):  Default: 0.
            presentation_change_count (int | Unset):  Default: 0.
            rules (list[RuleDiffDTO] | Unset):
            steps (list[StepDiffDTO] | Unset):
            edges (list[EdgeDiffDTO] | Unset):
            contracts (list[ContractChangeDTO] | Unset):
            diagnostics (list[GraphDiagnosticDTO] | Unset):
            activation_blocked (bool | Unset):  Default: False.
            activation_blockers (list[str] | Unset):
    """

    target: ArtifactRefDTO
    success: bool | Unset = True
    base: ArtifactRefDTO | None | Unset = UNSET
    executable_change: bool | Unset = False
    semantic_change_count: int | Unset = 0
    presentation_change_count: int | Unset = 0
    rules: list[RuleDiffDTO] | Unset = UNSET
    steps: list[StepDiffDTO] | Unset = UNSET
    edges: list[EdgeDiffDTO] | Unset = UNSET
    contracts: list[ContractChangeDTO] | Unset = UNSET
    diagnostics: list[GraphDiagnosticDTO] | Unset = UNSET
    activation_blocked: bool | Unset = False
    activation_blockers: list[str] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.artifact_ref_dto import ArtifactRefDTO

        target = self.target.to_dict()

        success = self.success

        base: dict[str, Any] | None | Unset
        if isinstance(self.base, Unset):
            base = UNSET
        elif isinstance(self.base, ArtifactRefDTO):
            base = self.base.to_dict()
        else:
            base = self.base

        executable_change = self.executable_change

        semantic_change_count = self.semantic_change_count

        presentation_change_count = self.presentation_change_count

        rules: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.rules, Unset):
            rules = []
            for rules_item_data in self.rules:
                rules_item = rules_item_data.to_dict()
                rules.append(rules_item)

        steps: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.steps, Unset):
            steps = []
            for steps_item_data in self.steps:
                steps_item = steps_item_data.to_dict()
                steps.append(steps_item)

        edges: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.edges, Unset):
            edges = []
            for edges_item_data in self.edges:
                edges_item = edges_item_data.to_dict()
                edges.append(edges_item)

        contracts: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.contracts, Unset):
            contracts = []
            for contracts_item_data in self.contracts:
                contracts_item = contracts_item_data.to_dict()
                contracts.append(contracts_item)

        diagnostics: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.diagnostics, Unset):
            diagnostics = []
            for diagnostics_item_data in self.diagnostics:
                diagnostics_item = diagnostics_item_data.to_dict()
                diagnostics.append(diagnostics_item)

        activation_blocked = self.activation_blocked

        activation_blockers: list[str] | Unset = UNSET
        if not isinstance(self.activation_blockers, Unset):
            activation_blockers = self.activation_blockers

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "target": target,
            }
        )
        if success is not UNSET:
            field_dict["success"] = success
        if base is not UNSET:
            field_dict["base"] = base
        if executable_change is not UNSET:
            field_dict["executable_change"] = executable_change
        if semantic_change_count is not UNSET:
            field_dict["semantic_change_count"] = semantic_change_count
        if presentation_change_count is not UNSET:
            field_dict["presentation_change_count"] = presentation_change_count
        if rules is not UNSET:
            field_dict["rules"] = rules
        if steps is not UNSET:
            field_dict["steps"] = steps
        if edges is not UNSET:
            field_dict["edges"] = edges
        if contracts is not UNSET:
            field_dict["contracts"] = contracts
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics
        if activation_blocked is not UNSET:
            field_dict["activation_blocked"] = activation_blocked
        if activation_blockers is not UNSET:
            field_dict["activation_blockers"] = activation_blockers

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_ref_dto import ArtifactRefDTO
        from ..models.contract_change_dto import ContractChangeDTO
        from ..models.edge_diff_dto import EdgeDiffDTO
        from ..models.graph_diagnostic_dto import GraphDiagnosticDTO
        from ..models.rule_diff_dto import RuleDiffDTO
        from ..models.step_diff_dto import StepDiffDTO

        d = dict(src_dict)
        target = ArtifactRefDTO.from_dict(d.pop("target"))

        success = d.pop("success", UNSET)

        def _parse_base(data: object) -> ArtifactRefDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                base_type_0 = ArtifactRefDTO.from_dict(data)

                return base_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(ArtifactRefDTO | None | Unset, data)

        base = _parse_base(d.pop("base", UNSET))

        executable_change = d.pop("executable_change", UNSET)

        semantic_change_count = d.pop("semantic_change_count", UNSET)

        presentation_change_count = d.pop("presentation_change_count", UNSET)

        _rules = d.pop("rules", UNSET)
        rules: list[RuleDiffDTO] | Unset = UNSET
        if _rules is not UNSET:
            rules = []
            for rules_item_data in _rules:
                rules_item = RuleDiffDTO.from_dict(rules_item_data)

                rules.append(rules_item)

        _steps = d.pop("steps", UNSET)
        steps: list[StepDiffDTO] | Unset = UNSET
        if _steps is not UNSET:
            steps = []
            for steps_item_data in _steps:
                steps_item = StepDiffDTO.from_dict(steps_item_data)

                steps.append(steps_item)

        _edges = d.pop("edges", UNSET)
        edges: list[EdgeDiffDTO] | Unset = UNSET
        if _edges is not UNSET:
            edges = []
            for edges_item_data in _edges:
                edges_item = EdgeDiffDTO.from_dict(edges_item_data)

                edges.append(edges_item)

        _contracts = d.pop("contracts", UNSET)
        contracts: list[ContractChangeDTO] | Unset = UNSET
        if _contracts is not UNSET:
            contracts = []
            for contracts_item_data in _contracts:
                contracts_item = ContractChangeDTO.from_dict(contracts_item_data)

                contracts.append(contracts_item)

        _diagnostics = d.pop("diagnostics", UNSET)
        diagnostics: list[GraphDiagnosticDTO] | Unset = UNSET
        if _diagnostics is not UNSET:
            diagnostics = []
            for diagnostics_item_data in _diagnostics:
                diagnostics_item = GraphDiagnosticDTO.from_dict(diagnostics_item_data)

                diagnostics.append(diagnostics_item)

        activation_blocked = d.pop("activation_blocked", UNSET)

        activation_blockers = cast(list[str], d.pop("activation_blockers", UNSET))

        playbook_artifact_diff_response = cls(
            target=target,
            success=success,
            base=base,
            executable_change=executable_change,
            semantic_change_count=semantic_change_count,
            presentation_change_count=presentation_change_count,
            rules=rules,
            steps=steps,
            edges=edges,
            contracts=contracts,
            diagnostics=diagnostics,
            activation_blocked=activation_blocked,
            activation_blockers=activation_blockers,
        )

        return playbook_artifact_diff_response
