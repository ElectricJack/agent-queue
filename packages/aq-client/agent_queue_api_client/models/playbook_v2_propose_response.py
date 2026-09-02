from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar, cast

from attrs import define as _attrs_define

from ..types import UNSET, Unset

if TYPE_CHECKING:
    from ..models.compiler_diagnostic_counts_dto import CompilerDiagnosticCountsDTO
    from ..models.compiler_diagnostic_dto import CompilerDiagnosticDTO
    from ..models.playbook_v2_propose_response_artifact_type_0 import PlaybookV2ProposeResponseArtifactType0
    from ..models.playbook_v2_propose_response_semantic_diff_type_0 import PlaybookV2ProposeResponseSemanticDiffType0


T = TypeVar("T", bound="PlaybookV2ProposeResponse")


@_attrs_define
class PlaybookV2ProposeResponse:
    """
    Attributes:
        success (bool):
        activatable (bool):
        source_digest (str):
        compiler_build (str):
        counts (CompilerDiagnosticCountsDTO):
        artifact_sha256 (None | str | Unset):
        contract_fingerprint (None | str | Unset):
        diagnostics (list[CompilerDiagnosticDTO] | Unset):
        semantic_diff (None | PlaybookV2ProposeResponseSemanticDiffType0 | Unset):
        artifact (None | PlaybookV2ProposeResponseArtifactType0 | Unset):
    """

    success: bool
    activatable: bool
    source_digest: str
    compiler_build: str
    counts: CompilerDiagnosticCountsDTO
    artifact_sha256: None | str | Unset = UNSET
    contract_fingerprint: None | str | Unset = UNSET
    diagnostics: list[CompilerDiagnosticDTO] | Unset = UNSET
    semantic_diff: None | PlaybookV2ProposeResponseSemanticDiffType0 | Unset = UNSET
    artifact: None | PlaybookV2ProposeResponseArtifactType0 | Unset = UNSET

    def to_dict(self) -> dict[str, Any]:
        from ..models.playbook_v2_propose_response_artifact_type_0 import PlaybookV2ProposeResponseArtifactType0
        from ..models.playbook_v2_propose_response_semantic_diff_type_0 import (
            PlaybookV2ProposeResponseSemanticDiffType0,
        )

        success = self.success

        activatable = self.activatable

        source_digest = self.source_digest

        compiler_build = self.compiler_build

        counts = self.counts.to_dict()

        artifact_sha256: None | str | Unset
        if isinstance(self.artifact_sha256, Unset):
            artifact_sha256 = UNSET
        else:
            artifact_sha256 = self.artifact_sha256

        contract_fingerprint: None | str | Unset
        if isinstance(self.contract_fingerprint, Unset):
            contract_fingerprint = UNSET
        else:
            contract_fingerprint = self.contract_fingerprint

        diagnostics: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.diagnostics, Unset):
            diagnostics = []
            for diagnostics_item_data in self.diagnostics:
                diagnostics_item = diagnostics_item_data.to_dict()
                diagnostics.append(diagnostics_item)

        semantic_diff: dict[str, Any] | None | Unset
        if isinstance(self.semantic_diff, Unset):
            semantic_diff = UNSET
        elif isinstance(self.semantic_diff, PlaybookV2ProposeResponseSemanticDiffType0):
            semantic_diff = self.semantic_diff.to_dict()
        else:
            semantic_diff = self.semantic_diff

        artifact: dict[str, Any] | None | Unset
        if isinstance(self.artifact, Unset):
            artifact = UNSET
        elif isinstance(self.artifact, PlaybookV2ProposeResponseArtifactType0):
            artifact = self.artifact.to_dict()
        else:
            artifact = self.artifact

        field_dict: dict[str, Any] = {}

        field_dict.update(
            {
                "success": success,
                "activatable": activatable,
                "source_digest": source_digest,
                "compiler_build": compiler_build,
                "counts": counts,
            }
        )
        if artifact_sha256 is not UNSET:
            field_dict["artifact_sha256"] = artifact_sha256
        if contract_fingerprint is not UNSET:
            field_dict["contract_fingerprint"] = contract_fingerprint
        if diagnostics is not UNSET:
            field_dict["diagnostics"] = diagnostics
        if semantic_diff is not UNSET:
            field_dict["semantic_diff"] = semantic_diff
        if artifact is not UNSET:
            field_dict["artifact"] = artifact

        return field_dict

    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.compiler_diagnostic_counts_dto import CompilerDiagnosticCountsDTO
        from ..models.compiler_diagnostic_dto import CompilerDiagnosticDTO
        from ..models.playbook_v2_propose_response_artifact_type_0 import PlaybookV2ProposeResponseArtifactType0
        from ..models.playbook_v2_propose_response_semantic_diff_type_0 import (
            PlaybookV2ProposeResponseSemanticDiffType0,
        )

        d = dict(src_dict)
        success = d.pop("success")

        activatable = d.pop("activatable")

        source_digest = d.pop("source_digest")

        compiler_build = d.pop("compiler_build")

        counts = CompilerDiagnosticCountsDTO.from_dict(d.pop("counts"))

        def _parse_artifact_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        artifact_sha256 = _parse_artifact_sha256(d.pop("artifact_sha256", UNSET))

        def _parse_contract_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        contract_fingerprint = _parse_contract_fingerprint(d.pop("contract_fingerprint", UNSET))

        _diagnostics = d.pop("diagnostics", UNSET)
        diagnostics: list[CompilerDiagnosticDTO] | Unset = UNSET
        if _diagnostics is not UNSET:
            diagnostics = []
            for diagnostics_item_data in _diagnostics:
                diagnostics_item = CompilerDiagnosticDTO.from_dict(diagnostics_item_data)

                diagnostics.append(diagnostics_item)

        def _parse_semantic_diff(data: object) -> None | PlaybookV2ProposeResponseSemanticDiffType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                semantic_diff_type_0 = PlaybookV2ProposeResponseSemanticDiffType0.from_dict(data)

                return semantic_diff_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookV2ProposeResponseSemanticDiffType0 | Unset, data)

        semantic_diff = _parse_semantic_diff(d.pop("semantic_diff", UNSET))

        def _parse_artifact(data: object) -> None | PlaybookV2ProposeResponseArtifactType0 | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                artifact_type_0 = PlaybookV2ProposeResponseArtifactType0.from_dict(data)

                return artifact_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | PlaybookV2ProposeResponseArtifactType0 | Unset, data)

        artifact = _parse_artifact(d.pop("artifact", UNSET))

        playbook_v2_propose_response = cls(
            success=success,
            activatable=activatable,
            source_digest=source_digest,
            compiler_build=compiler_build,
            counts=counts,
            artifact_sha256=artifact_sha256,
            contract_fingerprint=contract_fingerprint,
            diagnostics=diagnostics,
            semantic_diff=semantic_diff,
            artifact=artifact,
        )

        return playbook_v2_propose_response
