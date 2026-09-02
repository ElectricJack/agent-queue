from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..models.graph_diagnostic_dto_severity import GraphDiagnosticDTOSeverity
from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.source_ref_dto import SourceRefDTO


T = TypeVar("T", bound="GraphDiagnosticDTO")


@_attrs_define
class GraphDiagnosticDTO:
    """A compile question, invalid reference, stale contract or disabled
    activation.  Diagnostics annotate the graph; they never hide it.

        Attributes:
            severity (GraphDiagnosticDTOSeverity):
            code (str):
            message (str):
            rule_id (None | str | Unset):
            step_id (None | str | Unset):
            source (None | SourceRefDTO | Unset):
    """

    severity: GraphDiagnosticDTOSeverity
    code: str
    message: str
    rule_id: None | str | Unset = UNSET
    step_id: None | str | Unset = UNSET
    source: None | SourceRefDTO | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.source_ref_dto import SourceRefDTO

        severity = self.severity.value

        code = self.code

        message = self.message

        rule_id: None | str | Unset
        if isinstance(self.rule_id, Unset):
            rule_id = UNSET
        else:
            rule_id = self.rule_id

        step_id: None | str | Unset
        if isinstance(self.step_id, Unset):
            step_id = UNSET
        else:
            step_id = self.step_id

        source: dict[str, Any] | None | Unset
        if isinstance(self.source, Unset):
            source = UNSET
        elif isinstance(self.source, SourceRefDTO):
            source = self.source.to_dict()
        else:
            source = self.source

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "severity": severity,
                "code": code,
                "message": message,
            }
        )
        if rule_id is not UNSET:
            field_dict["rule_id"] = rule_id
        if step_id is not UNSET:
            field_dict["step_id"] = step_id
        if source is not UNSET:
            field_dict["source"] = source

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.source_ref_dto import SourceRefDTO

        d = dict(src_dict)
        severity = GraphDiagnosticDTOSeverity(d.pop("severity"))

        code = d.pop("code")

        message = d.pop("message")

        def _parse_rule_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        rule_id = _parse_rule_id(d.pop("rule_id", UNSET))

        def _parse_step_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        step_id = _parse_step_id(d.pop("step_id", UNSET))

        def _parse_source(data: object) -> None | SourceRefDTO | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                source_type_0 = SourceRefDTO.from_dict(data)

                return source_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SourceRefDTO | Unset, data)

        source = _parse_source(d.pop("source", UNSET))

        graph_diagnostic_dto = cls(
            severity=severity,
            code=code,
            message=message,
            rule_id=rule_id,
            step_id=step_id,
            source=source,
        )

        return graph_diagnostic_dto
