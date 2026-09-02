from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.graph_node_dto_step_kind import GraphNodeDTOStepKind
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.ai_node_detail_dto import AiNodeDetailDTO
    from ..models.graph_diagnostic_dto import GraphDiagnosticDTO
    from ..models.grid_position_dto import GridPositionDTO
    from ..models.loop_node_detail_dto import LoopNodeDetailDTO
    from ..models.node_advanced_dto import NodeAdvancedDTO
    from ..models.node_badge_dto import NodeBadgeDTO
    from ..models.source_ref_dto import SourceRefDTO
    from ..models.step_explanation_dto import StepExplanationDTO
    from ..models.wait_node_detail_dto import WaitNodeDetailDTO


T = TypeVar("T", bound="GraphNodeDTO")


@_attrs_define
class GraphNodeDTO:
    """
    Attributes:
        id (str):
        rule_id (str):
        step_kind (GraphNodeDTOStepKind):
        title (str):
        explanation (StepExplanationDTO): The contract-derived intent card.  Node card and inspector consume
            this same object (design spec UI invariant).

            ``renderer="canonical"`` is the spec's lossless fallback: presentation
            metadata was absent, so every executable field is shown as a field/value
            pair.  It is a display fact, never a reason to hide a field, and never
            blocks activation.
        source (SourceRefDTO): Where in the authoring Markdown this element came from.
        advanced (NodeAdvancedDTO): Advanced view.  Canonical data, never the default explanation.
        description (None | str | Unset):
        entry (bool | Unset):  Default: False.
        terminal_outcome (None | str | Unset):
        badges (list[NodeBadgeDTO] | Unset):
        ai (AiNodeDetailDTO | None | Unset):
        loop (LoopNodeDetailDTO | None | Unset):
        wait (None | Unset | WaitNodeDetailDTO):
        diagnostics (list[GraphDiagnosticDTO] | Unset):
        out_degree (int | Unset):  Default: 0.
        position (GridPositionDTO | Unset):
    """

    id: str
    rule_id: str
    step_kind: GraphNodeDTOStepKind
    title: str
    explanation: StepExplanationDTO
    source: SourceRefDTO
    advanced: NodeAdvancedDTO
    description: None | str | Unset = UNSET
    entry: bool | Unset = False
    terminal_outcome: None | str | Unset = UNSET
    badges: list[NodeBadgeDTO] | Unset = UNSET
    ai: AiNodeDetailDTO | None | Unset = UNSET
    loop: LoopNodeDetailDTO | None | Unset = UNSET
    wait: None | Unset | WaitNodeDetailDTO = UNSET
    diagnostics: list[GraphDiagnosticDTO] | Unset = UNSET
    out_degree: int | Unset = 0
    position: GridPositionDTO | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.ai_node_detail_dto import AiNodeDetailDTO
        from ..models.loop_node_detail_dto import LoopNodeDetailDTO
        from ..models.wait_node_detail_dto import WaitNodeDetailDTO

        id = self.id

        rule_id = self.rule_id

        step_kind = self.step_kind.value

        title = self.title

        explanation = self.explanation.to_dict()

        source = self.source.to_dict()

        advanced = self.advanced.to_dict()

        description: None | str | Unset
        if isinstance(self.description, Unset):
            description = UNSET
        else:
            description = self.description

        entry = self.entry

        terminal_outcome: None | str | Unset
        if isinstance(self.terminal_outcome, Unset):
            terminal_outcome = UNSET
        else:
            terminal_outcome = self.terminal_outcome

        badges: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.badges, Unset):
            badges = []
            for badges_item_data in self.badges:
                badges_item = badges_item_data.to_dict()
                badges.append(badges_item)

        ai: dict[str, Any] | None | Unset
        if isinstance(self.ai, Unset):
            ai = UNSET
        elif isinstance(self.ai, AiNodeDetailDTO):
            ai = self.ai.to_dict()
        else:
            ai = self.ai

        loop: dict[str, Any] | None | Unset
        if isinstance(self.loop, Unset):
            loop = UNSET
        elif isinstance(self.loop, LoopNodeDetailDTO):
            loop = self.loop.to_dict()
        else:
            loop = self.loop

        wait: dict[str, Any] | None | Unset
        if isinstance(self.wait, Unset):
            wait = UNSET
        elif isinstance(self.wait, WaitNodeDetailDTO):
            wait = self.wait.to_dict()
        else:
            wait = self.wait

        diagnostics: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.diagnostics, Unset):
            diagnostics = []
            for diagnostics_item_data in self.diagnostics:
                diagnostics_item = diagnostics_item_data.to_dict()
                diagnostics.append(diagnostics_item)

        out_degree = self.out_degree

        position: dict[str, Any] | Unset = UNSET
        if not isinstance(self.position, Unset):
            position = self.position.to_dict()

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "id": id,
                "rule_id": rule_id,
                "step_kind": step_kind,
                "title": title,
                "explanation": explanation,
                "source": source,
                "advanced": advanced,
            }
        )
        if description is not UNSET:
            field_dict["description"] = description
        if entry is not UNSET:
            field_dict["entry"] = entry
        if terminal_outcome is not UNSET:
            field_dict["terminal_outcome"] = terminal_outcome
        if badges is not UNSET:
            field_dict["badges"] = badges
        if ai is not UNSET:
            field_dict["ai"] = ai
        if loop is not UNSET:
            field_dict["loop"] = loop
        if wait is not UNSET:
            field_dict["wait"] = wait
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics
        if out_degree is not UNSET:
            field_dict["out_degree"] = out_degree
        if position is not UNSET:
            field_dict["position"] = position

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.ai_node_detail_dto import AiNodeDetailDTO
        from ..models.graph_diagnostic_dto import GraphDiagnosticDTO
        from ..models.grid_position_dto import GridPositionDTO
        from ..models.loop_node_detail_dto import LoopNodeDetailDTO
        from ..models.node_advanced_dto import NodeAdvancedDTO
        from ..models.node_badge_dto import NodeBadgeDTO
        from ..models.source_ref_dto import SourceRefDTO
        from ..models.step_explanation_dto import StepExplanationDTO
        from ..models.wait_node_detail_dto import WaitNodeDetailDTO

        d = dict(src_dict)
        id = d.pop("id")

        rule_id = d.pop("rule_id")

        step_kind = GraphNodeDTOStepKind(d.pop("step_kind"))

        title = d.pop("title")

        explanation = StepExplanationDTO.from_dict(d.pop("explanation"))

        source = SourceRefDTO.from_dict(d.pop("source"))

        advanced = NodeAdvancedDTO.from_dict(d.pop("advanced"))

        def _parse_description(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        description = _parse_description(d.pop("description", UNSET))

        entry = d.pop("entry", UNSET)

        def _parse_terminal_outcome(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        terminal_outcome = _parse_terminal_outcome(d.pop("terminal_outcome", UNSET))

        _badges = d.pop("badges", UNSET)
        badges: list[NodeBadgeDTO] | Unset = UNSET
        if _badges is not UNSET:
            badges = []
            for badges_item_data in _badges:
                badges_item = NodeBadgeDTO.from_dict(badges_item_data)

                badges.append(badges_item)

        def _parse_ai(data: object) -> AiNodeDetailDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                ai_type_0 = AiNodeDetailDTO.from_dict(data)

                return ai_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AiNodeDetailDTO | None | Unset, data)

        ai = _parse_ai(d.pop("ai", UNSET))

        def _parse_loop(data: object) -> LoopNodeDetailDTO | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                loop_type_0 = LoopNodeDetailDTO.from_dict(data)

                return loop_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(LoopNodeDetailDTO | None | Unset, data)

        loop = _parse_loop(d.pop("loop", UNSET))

        def _parse_wait(data: object) -> None | Unset | WaitNodeDetailDTO:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                wait_type_0 = WaitNodeDetailDTO.from_dict(data)

                return wait_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | WaitNodeDetailDTO, data)

        wait = _parse_wait(d.pop("wait", UNSET))

        _diagnostics = d.pop("diagnostics", UNSET)
        diagnostics: list[GraphDiagnosticDTO] | Unset = UNSET
        if _diagnostics is not UNSET:
            diagnostics = []
            for diagnostics_item_data in _diagnostics:
                diagnostics_item = GraphDiagnosticDTO.from_dict(diagnostics_item_data)

                diagnostics.append(diagnostics_item)

        out_degree = d.pop("out_degree", UNSET)

        _position = d.pop("position", UNSET)
        position: GridPositionDTO | Unset
        if isinstance(_position, Unset):
            position = UNSET
        else:
            position = GridPositionDTO.from_dict(_position)

        graph_node_dto = cls(
            id=id,
            rule_id=rule_id,
            step_kind=step_kind,
            title=title,
            explanation=explanation,
            source=source,
            advanced=advanced,
            description=description,
            entry=entry,
            terminal_outcome=terminal_outcome,
            badges=badges,
            ai=ai,
            loop=loop,
            wait=wait,
            diagnostics=diagnostics,
            out_degree=out_degree,
            position=position,
        )

        return graph_node_dto
