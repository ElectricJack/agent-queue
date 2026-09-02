from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.graph_diagnostic_dto import GraphDiagnosticDTO
    from ..models.rule_cluster_dto_trigger_filter_type_0 import RuleClusterDTOTriggerFilterType0
    from ..models.source_ref_dto import SourceRefDTO


T = TypeVar("T", bound="RuleClusterDTO")


@_attrs_define
class RuleClusterDTO:
    """One first-class rule.  A rule owns a closed subgraph — no edge in
    ``GraphEdgeDTO`` ever crosses ``rule_id``.

        Attributes:
            rule_id (str):
            name (str):
            event_type (str):
            entry_step_id (str):
            source (SourceRefDTO): Where in the authoring Markdown this element came from.
            trigger_filter (None | RuleClusterDTOTriggerFilterType0 | Unset):
            step_ids (list[str] | Unset):
            diagnostics (list[GraphDiagnosticDTO] | Unset):
    """

    rule_id: str
    name: str
    event_type: str
    entry_step_id: str
    source: SourceRefDTO
    trigger_filter: None | RuleClusterDTOTriggerFilterType0 | Unset = UNSET
    step_ids: list[str] | Unset = UNSET
    diagnostics: list[GraphDiagnosticDTO] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.rule_cluster_dto_trigger_filter_type_0 import RuleClusterDTOTriggerFilterType0

        rule_id = self.rule_id

        name = self.name

        event_type = self.event_type

        entry_step_id = self.entry_step_id

        source = self.source.to_dict()

        trigger_filter: dict[str, Any] | None | Unset
        if isinstance(self.trigger_filter, Unset):
            trigger_filter = UNSET
        elif isinstance(self.trigger_filter, RuleClusterDTOTriggerFilterType0):
            trigger_filter = self.trigger_filter.to_dict()
        else:
            trigger_filter = self.trigger_filter

        step_ids: list[str] | Unset = UNSET
        if not isinstance(self.step_ids, Unset):
            step_ids = self.step_ids

        diagnostics: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.diagnostics, Unset):
            diagnostics = []
            for diagnostics_item_data in self.diagnostics:
                diagnostics_item = diagnostics_item_data.to_dict()
                diagnostics.append(diagnostics_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "rule_id": rule_id,
                "name": name,
                "event_type": event_type,
                "entry_step_id": entry_step_id,
                "source": source,
            }
        )
        if trigger_filter is not UNSET:
            field_dict["trigger_filter"] = trigger_filter
        if step_ids is not UNSET:
            field_dict["step_ids"] = step_ids
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.graph_diagnostic_dto import GraphDiagnosticDTO
        from ..models.rule_cluster_dto_trigger_filter_type_0 import RuleClusterDTOTriggerFilterType0
        from ..models.source_ref_dto import SourceRefDTO

        d = dict(src_dict)
        rule_id = d.pop("rule_id")

        name = d.pop("name")

        event_type = d.pop("event_type")

        entry_step_id = d.pop("entry_step_id")

        source = SourceRefDTO.from_dict(d.pop("source"))

        def _parse_trigger_filter(data: object) -> None | RuleClusterDTOTriggerFilterType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                trigger_filter_type_0 = RuleClusterDTOTriggerFilterType0.from_dict(data)

                return trigger_filter_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | RuleClusterDTOTriggerFilterType0 | Unset, data)

        trigger_filter = _parse_trigger_filter(d.pop("trigger_filter", UNSET))

        step_ids = cast(list[str], d.pop("step_ids", UNSET))

        _diagnostics = d.pop("diagnostics", UNSET)
        diagnostics: list[GraphDiagnosticDTO] | Unset = UNSET
        if _diagnostics is not UNSET:
            diagnostics = []
            for diagnostics_item_data in _diagnostics:
                diagnostics_item = GraphDiagnosticDTO.from_dict(diagnostics_item_data)

                diagnostics.append(diagnostics_item)

        rule_cluster_dto = cls(
            rule_id=rule_id,
            name=name,
            event_type=event_type,
            entry_step_id=entry_step_id,
            source=source,
            trigger_filter=trigger_filter,
            step_ids=step_ids,
            diagnostics=diagnostics,
        )

        return rule_cluster_dto
