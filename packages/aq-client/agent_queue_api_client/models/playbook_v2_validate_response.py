from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.compiler_diagnostic_counts_dto import CompilerDiagnosticCountsDTO
    from ..models.compiler_diagnostic_dto import CompilerDiagnosticDTO


T = TypeVar("T", bound="PlaybookV2ValidateResponse")


@_attrs_define
class PlaybookV2ValidateResponse:
    """
    Attributes:
        success (bool):
        counts (CompilerDiagnosticCountsDTO):
        artifact_sha256 (None | str | Unset):
        diagnostics (list[CompilerDiagnosticDTO] | Unset):
    """

    success: bool
    counts: CompilerDiagnosticCountsDTO
    artifact_sha256: None | str | Unset = UNSET
    diagnostics: list[CompilerDiagnosticDTO] | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        success = self.success

        counts = self.counts.to_dict()

        artifact_sha256: None | str | Unset
        if isinstance(self.artifact_sha256, Unset):
            artifact_sha256 = UNSET
        else:
            artifact_sha256 = self.artifact_sha256

        diagnostics: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.diagnostics, Unset):
            diagnostics = []
            for diagnostics_item_data in self.diagnostics:
                diagnostics_item = diagnostics_item_data.to_dict()
                diagnostics.append(diagnostics_item)

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "success": success,
                "counts": counts,
            }
        )
        if artifact_sha256 is not UNSET:
            field_dict["artifact_sha256"] = artifact_sha256
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiler_diagnostic_counts_dto import CompilerDiagnosticCountsDTO
        from ..models.compiler_diagnostic_dto import CompilerDiagnosticDTO

        d = dict(src_dict)
        success = d.pop("success")

        counts = CompilerDiagnosticCountsDTO.from_dict(d.pop("counts"))

        def _parse_artifact_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        artifact_sha256 = _parse_artifact_sha256(d.pop("artifact_sha256", UNSET))

        _diagnostics = d.pop("diagnostics", UNSET)
        diagnostics: list[CompilerDiagnosticDTO] | Unset = UNSET
        if _diagnostics is not UNSET:
            diagnostics = []
            for diagnostics_item_data in _diagnostics:
                diagnostics_item = CompilerDiagnosticDTO.from_dict(diagnostics_item_data)

                diagnostics.append(diagnostics_item)

        playbook_v2_validate_response = cls(
            success=success,
            counts=counts,
            artifact_sha256=artifact_sha256,
            diagnostics=diagnostics,
        )

        return playbook_v2_validate_response
